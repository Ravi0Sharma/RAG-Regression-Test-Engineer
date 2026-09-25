"""CLI separates data preparation, generation, evaluation and reporting."""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

from .agent import run_agent
from .core import Budget, Config, Events, load_env, read_json, write_json
from .data import fetch_rows, import_tasks, prepare_source, public_task
from .demo import demo
from .evaluation import evaluate, report, review
from .model import OpenAIModel
from .retrieval import build_index
from .runner import DockerRunner, docker_command, prepare_image


def doctor():
    docker = shutil.which("docker")
    running = False
    if docker:
        try:
            running = subprocess.run([docker, "info"], capture_output=True, timeout=10).returncode == 0
        except subprocess.TimeoutExpired:
            pass
    return {"python": platform.python_version(), "architecture": platform.machine(),
            "docker_installed": bool(docker), "docker_running": running,
            "api_key_present": bool(os.environ.get("OPENAI_API_KEY")),
            "free_disk_gib": round(shutil.disk_usage(".").free / 1024**3, 1),
            "note": "ARM uses configured amd64 image emulation; verify one task before scaling."}


def run_one(task_directory, output, mode, config):
    directory, output = Path(task_directory), Path(output)
    if output.exists():
        raise ValueError("Run directory already exists; use a new path")
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY saknas. .env lämnas orörd.")
    task = public_task(read_json(directory / "task.json"))
    runtime = read_json(directory / "runtime.json")
    docker_command(["image", "inspect", runtime["image"]])
    if not (directory / "index.json").exists():
        raise ValueError("Build the index before generation")
    budget = Budget(config)
    events = Events(output / "events.jsonl")
    model = OpenAIModel(config, budget, events)
    runner = DockerRunner(task, config, runtime)
    return run_agent(directory, output, mode, config, model, budget, events, runner)


def parser():
    root = argparse.ArgumentParser(description="Single-agent RAG regression testing; .env is never written.")
    root.add_argument("--config", type=Path, default=Path("config.json"))
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check local runtime without exposing credentials")
    synthetic = commands.add_parser("demo", help="Offline synthetic end-to-end demonstration")
    synthetic.add_argument("--out", type=Path, default=Path("runs/demo"))
    data = commands.add_parser("dataset", help="Freeze a seeded SWE-bench selection")
    data.add_argument("--dataset", default="princeton-nlp/SWE-bench_Lite")
    data.add_argument("--split", choices=["dev", "test"], default="dev")
    data.add_argument("--count", type=int, default=5)
    data.add_argument("--seed", type=int, default=42)
    data.add_argument("--repo")
    data.add_argument("--input", type=Path, help="Complete local JSONL export instead of download")
    data.add_argument("--out", type=Path, required=True)
    prepare = commands.add_parser("prepare", help="Download immutable pre-fix source archive")
    prepare.add_argument("task", type=Path)
    image = commands.add_parser("image", help="Pull and pin an existing SWE-bench container image")
    image.add_argument("task", type=Path)
    image.add_argument("--image")
    image.add_argument("--backend", choices=["pytest", "unittest", "django"])
    index = commands.add_parser("index", help="Build traceable lexical or hybrid search index")
    index.add_argument("task", type=Path)
    index.add_argument("--max-tokens", type=int, default=5_000_000)
    index.add_argument("--max-api-calls", type=int, default=1000)
    index.add_argument("--max-seconds", type=int, default=1800)
    run = commands.add_parser("run", help="Generate without reading oracle/fixed version")
    run.add_argument("task", type=Path)
    run.add_argument("--mode", choices=["baseline", "agentic"], default="agentic")
    run.add_argument("--out", type=Path, required=True)
    batch = commands.add_parser("batch", help="Run both variants sequentially, generation only")
    batch.add_argument("tasks", type=Path)
    batch.add_argument("--out", type=Path, required=True)
    batch.add_argument("--max-total-usd", type=float, required=True, help="Explicit generation spending cap")
    evaluation = commands.add_parser("evaluate", help="Evaluate a frozen test in fresh before/after containers")
    evaluation.add_argument("run", type=Path)
    evaluation.add_argument("--oracle", type=Path, required=True)
    judgement = commands.add_parser("review", help="Record human assessment of bug relevance")
    judgement.add_argument("run", type=Path)
    judgement.add_argument("--verdict", choices=["relevant", "unrelated", "unclear"], required=True)
    judgement.add_argument("--reason", required=True)
    reporting = commands.add_parser("report", help="Compare variants; retain missing/error cases in denominator")
    reporting.add_argument("runs", type=Path)
    reporting.add_argument("--selection", type=Path, required=True)
    reporting.add_argument("--out", type=Path, required=True)
    return root


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        load_env()
        config = Config.load(arguments.config)
        command = arguments.command
        if command == "doctor":
            result = doctor()
        elif command == "demo":
            result = demo(arguments.out)
        elif command == "dataset":
            rows = ([json.loads(line) for line in arguments.input.read_text().splitlines() if line.strip()]
                    if arguments.input else fetch_rows(arguments.dataset, arguments.split))
            tasks = import_tasks(rows, arguments.out, arguments.count, arguments.seed,
                                 arguments.repo, arguments.dataset, arguments.split)
            result = {"selected": [t["instance_id"] for t in tasks]}
        elif command == "prepare":
            prepare_source(arguments.task)
            result = {"source": str(arguments.task / "source")}
        elif command == "image":
            prepare_image(arguments.task, arguments.image, config.docker_platform, arguments.backend)
            result = read_json(arguments.task / "runtime.json")
        elif command == "index":
            config = replace(config, max_tokens=arguments.max_tokens, max_api_calls=arguments.max_api_calls,
                             max_seconds=arguments.max_seconds)
            config.validate()
            budget = Budget(config)
            model = OpenAIModel(config, budget, Events(arguments.task / "index-events.jsonl")) if config.retrieval == "hybrid" else None
            index = build_index(arguments.task, config, model.embed if model else None)
            write_json(arguments.task / "index-usage.json", budget.summary())
            result = {"chunks": len(index["chunks"]), **budget.summary()}
        elif command == "run":
            result = run_one(arguments.task, arguments.out, arguments.mode, config)
        elif command == "batch":
            if arguments.out.exists() or arguments.max_total_usd <= 0:
                raise ValueError("Use a new output path and positive spending cap")
            selection = read_json(arguments.tasks / "selection.json")
            spent, completed = 0.0, []
            for identifier in selection["ids"]:
                for mode in ("baseline", "agentic"):
                    remaining = arguments.max_total_usd - spent
                    if remaining <= 0:
                        break
                    local_config = Config(**{**asdict(config), "max_cost_usd": min(config.max_cost_usd, remaining)})
                    summary = run_one(arguments.tasks / "public" / identifier,
                        arguments.out / mode / identifier, mode, local_config)
                    spent += summary["estimated_cost_usd"]
                    completed.append({"instance_id": identifier, "mode": mode, "stop_reason": summary["stop_reason"]})
                    write_json(arguments.out / "batch.json", {"completed": completed, "estimated_cost_usd": spent})
                    if summary["stop_reason"] == "run_error":
                        raise RuntimeError("Batch stopped on run error; inspect logs before restarting")
            result = {"completed": completed, "estimated_cost_usd": spent}
        elif command == "evaluate":
            result = evaluate(arguments.run, arguments.oracle)
        elif command == "review":
            review(arguments.run, arguments.verdict, arguments.reason)
            result = {"review": arguments.verdict}
        else:
            result = report(arguments.runs, arguments.selection, arguments.out)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print("Error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

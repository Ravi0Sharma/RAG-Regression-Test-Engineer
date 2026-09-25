"""Separate evaluator and human review. Never used as agent feedback."""

import csv
import json
from pathlib import Path

from .core import Config, digest, read_json, safe_relative, write_json
from .data import public_task
from .runner import DockerRunner


def verify_frozen(frozen):
    original = {key: value for key, value in frozen.items() if key != "artifact_sha256"}
    if digest(json.dumps(original, sort_keys=True)) != frozen["artifact_sha256"]:
        raise ValueError("Frozen artifact modified")
    if digest(frozen["code"]) != frozen["sha256"]:
        raise ValueError("Frozen test modified")
    if safe_relative(frozen["path"]).name != "test_rag_regression.py":
        raise ValueError("Invalid frozen test path")


def evaluate(run_directory, oracle_path, runner_factory=DockerRunner):
    run_directory = Path(run_directory)
    destination = run_directory / "evaluation"
    if destination.exists():
        raise ValueError("Evaluation already exists; preserve it and use a new generation run")
    frozen = read_json(run_directory / "frozen.json")
    task = public_task(read_json(run_directory / "task.json"))
    oracle = read_json(oracle_path)
    if (frozen["instance_id"] != task["instance_id"] or oracle["instance_id"] != task["instance_id"]
        or frozen["base_commit"] != task["base_commit"] or oracle["base_commit"] != task["base_commit"]):
        raise ValueError("Task, frozen test and oracle identity must match")
    verify_frozen(frozen)
    if not oracle["patch"].strip():
        raise ValueError("Missing reference fix")
    config = Config.load(run_directory / "config.json")
    runner = runner_factory(task, config, frozen["runtime"])
    destination.mkdir()
    before = runner.run(frozen, destination / "before", config.test_timeout)
    after = runner.run(frozen, destination / "after", config.test_timeout, patch=oracle["patch"])
    # A different test running after the fix cannot count as fail-to-pass.
    same_cases = bool(before["failed"]) and set(before["failed"]) <= set(after["passed"])
    transition = before["status"] == "test_failure" and after["status"] == "passed" and same_cases
    result = {"instance_id": task["instance_id"], "sha256": frozen["sha256"],
              "artifact_sha256": frozen["artifact_sha256"],
              "patch_sha256": digest(oracle["patch"]), "before": before, "after": after,
              "fail_to_pass": transition, "status": "pending_review" if transition else "not_successful"}
    write_json(destination / "result.json", result)
    return result


def review(run_directory, verdict, reason):
    directory = Path(run_directory)
    result = read_json(directory / "evaluation/result.json")
    frozen = read_json(directory / "frozen.json")
    verify_frozen(frozen)
    if frozen["artifact_sha256"] != result["artifact_sha256"] or not reason.strip():
        raise ValueError("Review requires unchanged test and a reason")
    if verdict not in {"relevant", "unrelated", "unclear"}:
        raise ValueError("Invalid verdict")
    if verdict == "relevant" and not result["fail_to_pass"]:
        raise ValueError("Cannot confirm success without matching fail-to-pass execution")
    write_json(directory / "evaluation/review.json", {"verdict": verdict, "reason": reason,
               "sha256": result["sha256"], "evaluation_sha256": digest((directory / "evaluation/result.json").read_bytes())})


def report(root, selection, destination):
    root, destination = Path(root), Path(destination)
    ids = read_json(selection)["ids"]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Selection must contain unique task IDs")
    found = {}
    for path in root.rglob("summary.json"):
        run = read_json(path)
        key = (run["instance_id"], run["mode"])
        if key[0] not in ids:
            continue
        if key in found:
            raise ValueError("Duplicate runs for same task/mode; report one experiment directory at a time")
        found[key] = (path.parent, run)
    configurations = []
    for directory, run in found.values():
        config = read_json(directory / "config.json")
        configurations.append({key: value for key, value in config.items() if key != "max_cost_usd"})
    if configurations and any(c != configurations[0] for c in configurations):
        raise ValueError("Experiment mixes models or settings; report matched configurations separately")
    rows = []
    for identifier in ids:
        for mode in ("baseline", "agentic"):
            row = {"instance_id": identifier, "mode": mode, "status": "missing", "success": False,
                   "evaluated": False, "test_versions": 0, "test_runs": 0, "search_rounds": 0, "api_calls": 0,
                   "generation_status": "not_run", "before_status": "not_run", "after_status": "not_run",
                   "seconds": 0, "input_tokens": 0, "output_tokens": 0,
                   "embedding_tokens": 0, "estimated_cost_usd": 0, "stop_reason": "not_run"}
            if (identifier, mode) in found:
                directory, run = found[identifier, mode]
                for key in list(row):
                    if key in run:
                        row[key] = run[key]
                row["status"] = "not_evaluated"
                row["generation_status"] = (run.get("last_result") or {}).get("status", "not_run")
                evaluation = directory / "evaluation/result.json"
                if evaluation.exists():
                    result = read_json(evaluation)
                    frozen = read_json(directory / "frozen.json")
                    verify_frozen(frozen)
                    if frozen["artifact_sha256"] != result["artifact_sha256"]:
                        raise ValueError("Evaluation no longer matches frozen artifact")
                    row["status"] = result["status"]
                    row["before_status"], row["after_status"] = result["before"]["status"], result["after"]["status"]
                    row["evaluated"] = all(result[side]["status"] in {"passed", "test_failure"} for side in ("before", "after"))
                    reviewed = directory / "evaluation/review.json"
                    if reviewed.exists():
                        judgement = read_json(reviewed)
                        if judgement["sha256"] != result["sha256"] or judgement["evaluation_sha256"] != digest(evaluation.read_bytes()):
                            raise ValueError("Review evidence changed")
                        row["success"] = result["fail_to_pass"] and judgement["verdict"] == "relevant"
                        row["status"] = "confirmed_success" if row["success"] else judgement["verdict"]
            rows.append(row)
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / "results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    totals = {}
    for mode in ("baseline", "agentic"):
        subset = [r for r in rows if r["mode"] == mode]
        totals[mode] = {"selected": len(ids), "evaluated": sum(r["evaluated"] for r in subset),
            "confirmed_success": sum(r["success"] for r in subset),
            "success_rate_all_selected": sum(r["success"] for r in subset) / len(ids),
            "estimated_generation_cost_usd": sum(r["estimated_cost_usd"] for r in subset),
            "seconds": sum(r["seconds"] for r in subset)}
    write_json(destination / "report.json", {"totals": totals, "rows": rows,
               "selection": read_json(selection), "configuration": configurations[0] if configurations else None})
    lines = ["# Experimentrapport", "", "Nämnare: alla valda uppgifter. Saknade körningar ligger kvar.",
             "Endast manuellt bekräftade fail-to-pass räknas som lyckade.", "",
             "| Metod | Valda | Utvärderade | Lyckade | Andel | API USD* |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for mode, value in totals.items():
        lines.append("| {} | {} | {} | {} | {:.1%} | {:.4f} |".format(mode, value["selected"], value["evaluated"],
            value["confirmed_success"], value["success_rate_all_selected"], value["estimated_generation_cost_usd"]))
    lines += ["", "*Konfigurerade tokenpriser; indexering, Docker och okänd kostnad vid API-avbrott ingår inte.",
              "Fullständiga feltyper, försök och stopporsaker finns i results.csv och körloggarna."]
    (destination / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return totals

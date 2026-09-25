"""One bounded agent. Decisions are structured; tools cannot access the oracle."""

from dataclasses import asdict
import json
from pathlib import Path

from .core import BudgetExceeded, digest, read_json, write_json
from .data import public_task
from .model import validate_schema, SCHEMAS
from .retrieval import Retriever, source_hash
from .runner import validate_test


def run_agent(task_directory, output, mode, config, model, budget, events, runner):
    directory, output = Path(task_directory), Path(output)
    if mode not in {"baseline", "agentic"}:
        raise ValueError("Unknown mode")
    # Only the explicit allowlist is ever passed to the model.
    task = public_task(read_json(directory / "task.json"))
    index = read_json(directory / "index.json")
    if index["source_sha256"] != source_hash(directory / "source"):
        raise ValueError("Source changed after indexing; prepare a clean snapshot")
    retriever = Retriever(index, task, config, getattr(model, "embed", None))
    max_search = 1 if mode == "baseline" else config.max_search_rounds
    max_versions = 1 if mode == "baseline" else config.max_test_versions
    searches = versions = stagnant = test_runs = 0
    previous_signature = None
    hits, test, result, candidate = [], None, None, False
    stop_reason, error = "unknown", None
    context = {"task": task, "test_backend": runner.runtime["backend"]}
    write_json(output / "config.json", asdict(config))
    write_json(output / "task.json", task)
    events.emit("start", task=task, mode=mode, source_sha256=index["source_sha256"])

    def ask(stage, extra):
        budget.check()
        answer = model.ask(stage, {**context, **extra})
        validate_schema(answer, SCHEMAS[stage])
        return answer

    try:
        route = ask("plan", {})
        action = "search"
        while True:
            budget.check()
            if action == "search":
                if searches >= max_search:
                    stop_reason = "search_budget"
                    break
                hits, trace = retriever.search(route["queries"], route["kinds"], route["path_glob"])
                searches += 1
                events.emit("search", round=searches, **trace)
                assessment = ask("assess", {"sources": hits, "previous_result": result})
                signature = digest(str([h["id"] for h in hits]) + str(assessment["missing"]))
                stagnant = stagnant + 1 if signature == previous_signature else 0
                previous_signature = signature
                if stagnant >= config.max_stagnant_steps:
                    stop_reason = "no_progress"
                    break
                if not assessment["sufficient"] and mode == "agentic":
                    route = assessment
                    continue
            if versions >= max_versions:
                stop_reason = "test_version_budget"
                break
            versions += 1
            proposal = ask("generate", {"sources": hits, "previous_test": test,
                                        "previous_result": result})
            try:
                validate_test(proposal, directory / "source")
                test = proposal
                test_runs += 1
                result = runner.run(test, output / ("attempt-" + str(versions)),
                                    timeout=min(config.test_timeout, budget.remaining_seconds))
            except ValueError as invalid:
                result = {"status": "invalid_test", "detail": str(invalid), "executed": 0}
            events.emit("test_result", version=versions, result=result)
            decision = ask("decide", {"test": proposal, "result": result, "sources": hits})
            events.emit("decision", version=versions, **decision)
            if decision["action"] == "candidate" and decision["relevant"] and result["status"] == "test_failure":
                candidate, stop_reason = True, "candidate"
                break
            if mode == "baseline":
                stop_reason = "single_pass_complete"
                break
            if decision["action"] == "stop":
                stop_reason = "agent_stop"
                break
            if versions >= max_versions:
                stop_reason = "test_version_budget"
                break
            signature = digest(proposal["code"] + result["status"])
            stagnant = stagnant + 1 if signature == previous_signature else 0
            previous_signature = signature
            if stagnant >= config.max_stagnant_steps:
                stop_reason = "no_progress"
                break
            route, action = decision, decision["action"]
            # A rejected candidate must gather evidence or revise, never silently pass.
            if action == "candidate":
                action = "revise"
    except BudgetExceeded as exceeded:
        stop_reason = str(exceeded)
    except (RuntimeError, ValueError, KeyError, OSError) as failure:
        stop_reason, error = "run_error", str(failure)
        events.emit("error", message=error)
    frozen = None
    if test is not None:
        frozen = {"path": test["path"], "code": test["code"],
                  "sha256": digest(test["code"]), "expected_failure": test["expected_failure"],
                  "instance_id": task["instance_id"], "base_commit": task["base_commit"],
                  "source_sha256": index["source_sha256"], "runtime": runner.runtime}
        frozen["artifact_sha256"] = digest(json.dumps(frozen, sort_keys=True))
        write_json(output / "frozen.json", frozen)
    summary = {"instance_id": task["instance_id"], "base_commit": task["base_commit"],
               "mode": mode, "model": config.model, "candidate": candidate,
               "stop_reason": stop_reason, "error": error, "search_rounds": searches,
               "test_versions": versions, "test_runs": test_runs, "last_result": result,
               "frozen_sha256": frozen["sha256"] if frozen else None, **budget.summary()}
    write_json(output / "summary.json", summary)
    events.emit("stop", **summary)
    return summary

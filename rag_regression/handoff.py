"""Durable, local manual model exchange. No model API, oracle reads or host test execution."""

from contextlib import contextmanager
from dataclasses import replace
import fcntl
import json
from pathlib import Path
import re
import time
from uuid import uuid4

from .core import Budget, Config, Events, digest, read_json, write_json
from .data import public_task
from .model import (SCHEMAS, SYSTEM, validate_schema, validate_source_request,
                    InvalidSourceRequest, MAX_SOURCE_REPAIRS, source_repair_context)
from .runner import DockerRunner, preflight_docker, check_runtime
from .workflow import Workflow

ROOT = Path(__file__).resolve().parent.parent
RUNS_ROOT = ROOT / "runs/handoff"
DEV_ROOT = ROOT / "data/dev"


def fingerprint(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False))


class Handoff:
    def __init__(self, runs=RUNS_ROOT, dev=DEV_ROOT, runner_factory=DockerRunner, *, selection_split="dev"):
        if selection_split not in {"dev", "test"}:
            raise ValueError("Expected dev or test selection_split")
        self.selection_split = selection_split
        self.runs, self.dev, self.runner_factory = Path(runs), Path(dev), runner_factory

    def directory(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ValueError("Expected a 32-character handoff run_id")
        path = self.runs / run_id
        if path.is_symlink():
            raise ValueError("Run directory cannot be a symlink")
        return path

    @contextmanager
    def locked(self, run_id):
        directory = self.directory(run_id)
        with (directory / "session.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield directory

    def workflow(self, directory, session):
        task_directory = self.dev / "public" / session["instance_id"]
        for name, expected in session["inputs"].items():
            if digest((task_directory / name).read_bytes()) != expected:
                raise ValueError("Prepared input changed: " + name)
        config = Config(**session["config"])
        config.validate()
        budget = Budget(config)
        budget.started -= session["active_seconds"]
        task = public_task(read_json(task_directory / "task.json"))
        runner = self.runner_factory(task, config, read_json(task_directory / "runtime.json"))
        return Workflow(task_directory, directory / "generation", config, budget,
                        Events(directory / "generation/events.jsonl"), runner)

    def start(self, instance_id, mode="agentic", config=None):
        selection = read_json(self.dev / "selection.json")
        if selection.get("split") != self.selection_split or instance_id not in selection["ids"]:
            raise ValueError("Select an instance from the frozen " + self.selection_split + " selection")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+", instance_id):
            raise ValueError("Invalid instance_id")
        task_directory = self.dev / "public" / instance_id
        if task_directory.resolve().parent != (self.dev / "public").resolve():
            raise ValueError("Task must be inside public dev data")
        config = replace(config or Config(), retrieval="lexical", model="manual-handoff")
        config.validate()
        if mode not in {"baseline", "agentic"}:
            raise ValueError("Unknown mode")
        task = public_task(read_json(task_directory / "task.json"))
        if task["instance_id"] != instance_id:
            raise ValueError("Task identity mismatch")
        runtime = read_json(task_directory / "runtime.json")
        runner = self.runner_factory(task, config, runtime)
        if self.runner_factory is DockerRunner:
            preflight_docker(runtime, "the same handoff start command")
        from dataclasses import asdict
        session = {"instance_id": instance_id, "selection_split": self.selection_split,
                   "config": asdict(config), "active_seconds": 0.0,
                   "created": time.time(), "revision": 0, "last_revision": None, "last_stage": None,
                   "request_count": 0, "pending": None, "response": None,
                   "inputs": {name: digest((task_directory / name).read_bytes())
                              for name in ("task.json", "index.json", "runtime.json")}}
        run_id = uuid4().hex
        directory = self.directory(run_id)
        if self.runner_factory is DockerRunner:
            check_runtime(runtime, directory / "runtime-check.json")
        # Validate the index and snapshot before creating any run files.
        workflow = Workflow(task_directory, directory / "generation", config, Budget(config),
                            Events(directory / "generation/events.jsonl"), runner)
        state = workflow.prepare(mode)
        session.update(run_id=run_id, state=state)
        write_json(directory / "session.json", session)
        return self.view(session)

    def view(self, session):
        return {**session["state"], "run_id": session["run_id"], "revision": session["revision"],
                "request": session["pending"], "response_imported": session["response"] is not None,
                "model_requests": session["request_count"], "active_seconds": session["active_seconds"]}

    def status(self, run_id):
        with self.locked(run_id) as directory:
            return self.view(read_json(directory / "session.json"))

    def validate_response(self, pending, response):
        if not isinstance(response, dict) or set(response) != {"request_id", "answer", "provenance"}:
            raise ValueError("Response needs request_id, answer and provenance")
        if response["request_id"] != pending["request_id"]:
            raise ValueError("Response belongs to another request or an earlier step")
        provenance = response["provenance"]
        if not isinstance(provenance, dict) or set(provenance) != {"provider", "model", "manual_edits", "note"}:
            raise ValueError("Provenance needs provider, model, manual_edits and note")
        if (any(not isinstance(provenance[k], str) or not provenance[k].strip() for k in ("provider", "model"))
            or type(provenance["manual_edits"]) is not bool or not isinstance(provenance["note"], str)):
            raise ValueError("Invalid response provenance")
        if provenance["manual_edits"] and not provenance["note"].strip():
            raise ValueError("Explain manual corrections in provenance.note")
        if len(json.dumps(response).encode()) > 200_000:
            raise ValueError("Response exceeds 200 KB")
        validate_schema(response["answer"], pending["schema"])
        stage, answer = pending["stage"], response["answer"]
        if stage == "assess" and "missing_categories" in answer and answer["sufficient"] == bool(answer["missing_categories"]):
            raise ValueError("Assessment must list missing categories exactly when insufficient")
        searches = stage == "plan" or (stage == "assess" and not answer["sufficient"]) or (stage == "decide" and answer["action"] == "search")
        if stage != "plan" and pending["context"].get("remaining_search_rounds", 1) == 0:
            searches = False  # Workflow's budget gate prevents another search.
        if searches and (not 1 <= len(answer["queries"]) <= pending["max_queries"]
                         or any(not q.strip() for q in answer["queries"]) or not answer["kinds"]):
            raise ValueError("A search needs nonempty queries within max_queries, and material kinds")
        validate_source_request(stage, answer, pending["context"], pending["max_queries"])

    def submit(self, run_id, response):
        """Import only. Execution occurs later in advance/Studio resume."""
        with self.locked(run_id) as directory:
            session = read_json(directory / "session.json")
            pending = session["pending"]
            if pending is None:
                raise ValueError("No model request is waiting")
            if session["response"] is not None and session["response"] != response:
                raise ValueError("A response was already imported; start a new run to change accepted history")
            try:
                self.validate_response(pending, response)
            except InvalidSourceRequest as invalid:
                write_json(directory / "exchange" / (pending["request_id"] + ".rejected.json"),
                           {"response": response, "error": str(invalid)})
                attempt = pending.get("repair_attempt", 0)
                if (attempt >= MAX_SOURCE_REPAIRS
                        or session["request_count"] >= session["config"]["max_api_calls"]
                        or session["active_seconds"] >= session["config"]["max_seconds"]):
                    reason = ("time_budget" if session["active_seconds"] >= session["config"]["max_seconds"]
                              else "model_request_budget" if session["request_count"] >= session["config"]["max_api_calls"]
                              else "invalid_source_request")
                    session["state"].update(next_node="freeze", stop_reason=reason, error=str(invalid))
                    session.update(pending=None, response=None, last_revision=session["revision"],
                                   last_stage=pending["stage"], revision=session["revision"] + 1)
                else:
                    packet = {k: v for k, v in pending.items() if k != "request_id"}
                    packet.update(repair_attempt=attempt + 1,
                                  context=source_repair_context(pending["context"], response["answer"], invalid))
                    packet["request_id"] = fingerprint(packet)
                    write_json(directory / "exchange" / (packet["request_id"] + ".request.json"), packet)
                    session.update(pending=packet, response=None, request_count=session["request_count"] + 1)
                write_json(directory / "session.json", session)
                return self.view(session)
            write_json(directory / "exchange" / (pending["request_id"] + ".response.json"), response)
            session["response"] = response
            write_json(directory / "session.json", session)
            return self.view(session)

    def step(self, run_id, revision=None, pause=False, expected_stage=None):
        """One durable step. Replaying the last committed revision does not repeat tools."""
        with self.locked(run_id) as directory:
            session = read_json(directory / "session.json")
            if revision is not None and revision != session["revision"]:
                if revision == session["last_revision"]:
                    return self.view(session)
                raise ValueError("Stale checkpoint; use current state, not time travel")
            if session["state"]["next_node"] == "end":
                return self.view(session)
            marker = directory / "inflight.json"
            if marker.exists():
                raise ValueError("Interrupted step has unresolved effects; preserve evidence and start a new run")
            original_stage = session["state"]["next_node"]
            if expected_stage is not None and original_stage != expected_stage:
                raise ValueError("Studio node does not match the saved workflow stage")
            started = time.monotonic()
            workflow = self.workflow(directory, session)
            state = workflow.gate(session["state"])
            stage = state["next_node"]
            if stage == "execute" and self.runner_factory is DockerRunner:
                preflight_docker(workflow.runner.runtime,
                                 "the same handoff advance/resume command")
            if stage in SCHEMAS and session["pending"] is None and session["request_count"] >= workflow.config.max_api_calls:
                state = workflow.stop(state, "model_request_budget")
                stage = "freeze"
            skip = stage != original_stage
            if stage in SCHEMAS:
                packet = {"run_id": run_id, "revision": session["revision"], "stage": stage,
                          "system": SYSTEM, "context": workflow.context(state), "schema": SCHEMAS[stage],
                          "max_queries": workflow.config.max_queries}
                packet["request_id"] = fingerprint(packet)
                if session["pending"] is None:
                    session["pending"] = packet
                    session["request_count"] += 1
                    write_json(directory / "exchange" / (packet["request_id"] + ".request.json"), packet)
                else:
                    # Prompt/schema updates apply only to newly exported requests.
                    # An existing immutable question remains resumable across upgrades.
                    old = session["pending"]
                    if old["run_id"] != run_id or old["revision"] != session["revision"] or old["stage"] != stage:
                        raise ValueError("Pending request identity changed")
                    if fingerprint({k: v for k, v in old.items() if k != "request_id"}) != old["request_id"]:
                        raise ValueError("Pending request content changed")
                    packet = old
                if pause or session["response"] is None:
                    session["active_seconds"] += time.monotonic() - started
                    write_json(directory / "session.json", session)
                    return self.view(session)
                self.validate_response(packet, session["response"])
                answer = session["response"]["answer"]
                workflow.events.emit("handoff_response", request_id=packet["request_id"],
                                     stage=stage, provenance=session["response"]["provenance"], result=answer)
            else:
                answer = None
            # A crash during a tool call must never silently execute it twice.
            write_json(marker, {"revision": session["revision"], "stage": stage})
            try:
                if not skip:
                    state = workflow.step(state, answer)
            except (RuntimeError, ValueError, KeyError, OSError) as error:
                if stage == "freeze":
                    raise
                workflow.events.emit("error", message=str(error))
                state = workflow.stop(state, "run_error", str(error))
            session["active_seconds"] += time.monotonic() - started
            session.update(state=state, last_revision=session["revision"], last_stage=stage,
                           revision=session["revision"] + 1, pending=None, response=None)
            if state["next_node"] == "end":
                state["summary"].update(manual_handoff=True, model_requests=session["request_count"],
                    accounting="local execution only; external model usage unknown",
                    external_usage={"input_tokens": None, "output_tokens": None, "cost_usd": None},
                    active_seconds=session["active_seconds"], wall_seconds=time.time() - session["created"])
                write_json(directory / "generation/summary.json", state["summary"])
            write_json(directory / "session.json", session)
            marker.unlink()
            return self.view(session)

    def advance(self, run_id):
        while True:
            view = self.step(run_id)
            if view["next_node"] == "end" or view["request"] is not None:
                return view

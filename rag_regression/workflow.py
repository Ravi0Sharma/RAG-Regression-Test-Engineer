"""Shared bounded steps. Model acquisition belongs to the CLI or handoff caller."""

from dataclasses import asdict
import json
from pathlib import Path

from .core import BudgetExceeded, digest, read_json, safe_relative, write_json
from .data import public_task
from .model import validate_answer, validate_source_request, SCHEMAS
from .retrieval import Retriever, source_hash
from .runner import validate_test

STAGES = ("plan", "search", "assess", "generate", "execute", "decide", "freeze")


def search_signature(route):
    """Comparable tool request, not a claim that paraphrases are semantically new."""
    if route.get("read_source_ids"):
        value = {"read_source_ids": sorted(set(route["read_source_ids"]))}
    else:
        value = {"queries": sorted(set(" ".join(q.lower().split()) for q in route.get("queries", []))),
                 "kinds": sorted(set(route.get("kinds", []))), "path_glob": route.get("path_glob") or "*"}
    return digest(json.dumps(value, sort_keys=True))


def select_sources(retriever, previous_sources, hits, trace, issue, top_k, bundle_anchor=None):
    """Fit new hits and one chosen test's bounded setup inside the source cap."""
    fresh = {hit["id"] for hit in hits}
    primary = hits[:top_k]
    fresh_examples = [key for key in trace.get("test_pattern_ids", []) if key in retriever.chunks]
    requested_examples = [key for key in trace.get("requested_ids", [])
                          if key in retriever.chunks and retriever.is_test_pattern(key)]
    # An explicit model request can change the chosen example. New search
    # ordering alone cannot silently replace the previous anchor.
    if requested_examples:
        bundle_anchor = requested_examples[0]
    elif bundle_anchor not in retriever.chunks or not retriever.is_test_pattern(bundle_anchor):
        bundle_anchor = next((source["id"] for source in previous_sources
                              if source["id"] in retriever.chunks and retriever.is_test_pattern(source["id"])), None)
    bundle_anchor = bundle_anchor or (fresh_examples[0] if fresh_examples else None)
    bundle_ids = ([bundle_anchor] + retriever.context_ids(bundle_anchor)) if bundle_anchor else []
    bundle_ids = list(dict.fromkeys(bundle_ids))
    bundle = [retriever.chunks[key] for key in bundle_ids if key in retriever.chunks]
    previous = retriever.rank_previous(previous_sources, issue)
    ordered = primary + bundle + previous + hits
    sources = list({hit["id"]: hit for hit in ordered}.values())[:2 * top_k]
    kept = {hit["id"] for hit in sources}
    previous_ids = {source["id"] for source in previous_sources}
    trace["bundle_anchor_id"] = bundle_anchor
    trace["bundle_ids"] = [hit["id"] for hit in bundle if hit["id"] in kept]
    trace["new_bundle_ids"] = [key for key in trace["bundle_ids"] if key not in previous_ids]
    trace["preserved_ids"] = [key for key in trace["bundle_ids"]
                              if key in previous_ids and key not in fresh]
    trace["bundle_dropped_ids"] = [key for key in bundle_ids if key not in kept]
    trace["delivered_ids"] = [hit["id"] for hit in sources]
    delivered = set(trace["delivered_ids"])
    context_ids = list(dict.fromkeys(trace.get("context_ids", []) +
                                     [key for key in bundle_ids if key != bundle_anchor]))
    trace["context_truncated"] = trace.get("context_truncated", False) or bool(set(context_ids) - delivered)
    trace["context_ids"] = [key for key in context_ids if key in delivered]
    trace["retained_ids"] = [hit["id"] for hit in sources
                             if hit["id"] in previous_ids and hit["id"] not in fresh]
    return sources, trace


class Workflow:
    def __init__(self, directory, output, config, budget, events, runner, embed=None):
        self.directory, self.output = Path(directory), Path(output)
        self.config, self.budget, self.events, self.runner = config, budget, events, runner
        self.task = public_task(read_json(self.directory / "task.json"))
        self.index = read_json(self.directory / "index.json")
        if self.index["source_sha256"] != source_hash(self.directory / "source"):
            raise ValueError("Source changed after indexing; prepare a clean snapshot")
        self.retriever = Retriever(self.index, self.task, config, embed)

    def prepare(self, mode):
        if mode not in {"baseline", "agentic"}:
            raise ValueError("Unknown mode")
        write_json(self.output / "config.json", asdict(self.config))
        write_json(self.output / "task.json", self.task)
        self.events.emit("start", task=self.task, mode=mode, source_sha256=self.index["source_sha256"])
        return {"task": self.task, "mode": mode, "route": {}, "sources": [], "search_trace": {},
                "assessment": {}, "proposal": {}, "test": None, "result": None, "decision": {},
                "model_input": {}, "candidate": False, "stop_reason": "unknown", "error": None,
                "search_rounds": 0, "test_versions": 0, "test_runs": 0,
                "stagnant": 0, "unresolved_rounds": 0, "search_history": [],
                "previous_signature": None, "bundle_anchor_id": None, "next_node": "plan",
                "frozen": None, "summary": {}, "explanation": "Förberett vald kodversion, sökindex och körmiljö."}

    def context(self, state):
        stage = state["next_node"]
        extra = {}
        if stage == "assess":
            extra = {"sources": state["sources"], "search_trace": state["search_trace"],
                     "previous_assessment": state["assessment"], "previous_result": state["result"],
                     "source_summary": {"available_count": len(state["sources"]),
                         "retained_from_previous_search": len(state["search_trace"].get("retained_ids", []))}}
        elif stage == "generate":
            extra = {"sources": state["sources"], "assessment": state["assessment"],
                     "search_trace": state["search_trace"],
                     "previous_test": state["proposal"] or None, "previous_result": state["result"]}
        elif stage == "decide":
            extra = {"test": state["proposal"], "result": state["result"], "sources": state["sources"]}
        return {"task": self.task, "test_backend": self.runner.runtime["backend"],
                "runner_setup": {
                    "source_tree": "read-only; create temporary fixtures outside it",
                    "django": "When backend=django: uses test_sqlite by default, calls django.setup(), then imports the standalone test module before DiscoverRunner database setup. Does not run the repository's runtests.py bootstrap.",
                    "django_database": "Use django.test.TestCase for ordinary database tests or TransactionTestCase for transaction/schema work. Plain unittest.TestCase does not request a Django test database; the supplied SQLite settings may have no NAME until test database setup. Query construction alone may need no database access.",
                    "django_apps": ("Use import paths or AppConfig paths supported by retrieved source; INSTALLED_APPS takes app paths, not guessed labels. "
                        "When the standalone runner needs a source-backed app for database setup, first confirm from settings/source that it is not already registered, "
                        "including under an alternate AppConfig path. Before importing models, use this module-level pattern:\n```python\n"
                        "from django.conf import settings\nfrom django.test.utils import override_settings\n\n"
                        "from django.apps import apps\nfrom django.apps.config import AppConfig\n\n"
                        "app_path = 'source.backed.app.path'  # replace with path from retrieved source\n"
                        "source_app = AppConfig.create(app_path)\n"
                        "app_paths = list(settings.INSTALLED_APPS)\n"
                        "if not any(config.name == source_app.name for config in apps.get_app_configs()):\n"
                        "    app_paths.append(app_path)\n"
                        "_rag_apps = override_settings(INSTALLED_APPS=app_paths)\n_rag_apps.enable()\n\n"
                        "# Import source models and define tests only after this point.\n\n"
                        "def tearDownModule():\n    _rag_apps.disable()\n```\n"
                        "Keep the override enabled through DiscoverRunner database setup. Preserve existing installed apps. "
                        "MIGRATION_MODULES selects a migrations module and does not register an app; available_apps limits a test's app registry "
                        "and does not register an app either. Add only apps evidenced by retrieved source, not guesses."),
                    "dependencies": "Runner dependency check does not establish task-specific fixtures or settings."},
                "remaining_search_rounds": max(0, self.limit(state, "search_rounds") - state["search_rounds"]),
                "remaining_test_versions": max(0, self.limit(state, "test_versions") - state["test_versions"]), **extra}

    def limit(self, state, kind):
        return 1 if state["mode"] == "baseline" else getattr(self.config, "max_" + kind)

    def stop(self, state, reason, error=None):
        return {**state, "stop_reason": reason, "error": error, "next_node": "freeze"}

    def gate(self, state):
        """Check before asking a model or running another tool, including after a pause."""
        if state["next_node"] in {"freeze", "end"}:
            return state
        try:
            self.budget.check()
        except BudgetExceeded as error:
            return self.stop(state, str(error))
        if state["next_node"] == "search" and state["search_rounds"] >= self.limit(state, "search_rounds"):
            return self.stop(state, "search_budget")
        if state["next_node"] == "generate" and state["test_versions"] >= self.limit(state, "test_versions"):
            return self.stop(state, "test_version_budget")
        return state

    def progress(self, state, signature):
        state["stagnant"] = state["stagnant"] + 1 if signature == state["previous_signature"] else 0
        state["previous_signature"] = signature
        return state["stagnant"] < self.config.max_stagnant_steps

    def step(self, state, answer=None):
        stage = state["next_node"]
        if stage not in STAGES:
            raise ValueError("Run is already complete")
        checked = self.gate(state)
        if checked["next_node"] != stage:
            return checked
        state = dict(state)
        if stage in SCHEMAS:
            validate_answer(stage, answer)
            validate_source_request(stage, answer, self.context(state), self.config.max_queries)
            state["model_input"] = self.context(state)
        state = getattr(self, stage)(state, answer)
        self.events.emit("node", node=stage, updates=state)
        return state

    def plan(self, state, answer):
        state.update(route=answer, next_node="search", explanation="Planen anger sökfrågor och filter.")
        return state

    def search(self, state, answer):
        route = state["route"]
        if route.get("read_source_ids"):
            # Use at most half the total source budget for expansion, leaving
            # room for earlier API/test evidence instead of replacing all of it.
            hits, trace = self.retriever.read_context(route["read_source_ids"], state["sources"],
                                                    limit=self.config.top_k)
        else:
            hits, trace = self.retriever.search(route["queries"], route["kinds"], route["path_glob"])
        sources, trace = select_sources(self.retriever, state["sources"], hits, trace,
                                       self.task["problem_statement"], self.config.top_k,
                                       state.get("bundle_anchor_id"))
        state["search_history"] = state.get("search_history", []) + [search_signature(route)]
        state.update(sources=sources, search_trace=trace, search_rounds=state["search_rounds"] + 1,
                     bundle_anchor_id=trace.get("bundle_anchor_id"),
                     next_node="assess", explanation=("Läst avgränsad kontext runt valda källor."
                         if route.get("read_source_ids") else "Sökt vald kodversion och fusionerat träfflistorna."))
        self.events.emit("search", round=state["search_rounds"], **trace)
        return state

    def assess(self, state, answer):
        previous = set(state["assessment"].get("missing_categories", []))
        current = set(answer.get("missing_categories", []))
        # Stable categories prevent rewording or unrelated new hits from resetting
        # unresolved evidence. A strict reduction or entirely different gap restarts it.
        repeated = bool(previous & current) and not current < previous
        state["unresolved_rounds"] = (state.get("unresolved_rounds", 0) + 1 if repeated else 1) if not answer["sufficient"] else 0
        state.update(assessment=answer, next_node="generate", explanation="Bedömt relevans, luckor och täckning.")
        signature = digest(str(sorted(h["id"] for h in state["sources"])))
        same_route = search_signature(answer) in state.get("search_history", [])
        progressed = self.progress(state, signature) if not answer["sufficient"] else True
        if not answer["sufficient"] and same_route and not progressed:
            return self.stop(state, "no_progress")
        if not answer["sufficient"] and state["mode"] == "agentic":
            if same_route and current and state["unresolved_rounds"] >= self.config.max_stagnant_steps:
                return self.stop(state, "unresolved_evidence")
            state.update(route=answer, next_node="search")
        return self.gate(state)

    def generate(self, state, answer):
        # The model selects a directory; the application owns the output basename.
        # Preserve the original answer in model/handoff events and log the adjustment.
        path = safe_relative(answer["path"])
        if path.name != "test_rag_regression.py":
            normalized = str(path.with_name("test_rag_regression.py"))
            self.events.emit("test_path_normalized", requested=answer["path"], path=normalized)
            answer = {**answer, "path": normalized}
        state.update(proposal=answer, test_versions=state["test_versions"] + 1,
                     next_node="execute", explanation="Tagit emot ett testförslag; ännu inte kört.")
        return state

    def execute(self, state, answer):
        proposal = state["proposal"]
        try:
            validate_test(proposal, self.directory / "source")
            state["test"] = proposal
            state["test_runs"] += 1
            state["result"] = self.runner.run(proposal, self.output / ("attempt-" + str(state["test_versions"])),
                timeout=min(self.config.test_timeout, self.budget.remaining_seconds))
        except ValueError as invalid:
            state["result"] = {"status": "invalid_test", "detail": str(invalid), "executed": 0}
        self.events.emit("test_result", version=state["test_versions"], result=state["result"])
        state.update(next_node="decide", explanation="Kört testförslaget; resultatet är en observation, inte ett relevansbetyg.")
        return state

    def decide(self, state, answer):
        state.update(decision=answer, route=answer, explanation="Valt nästa handling utifrån testresultatet.")
        self.events.emit("decision", version=state["test_versions"], **answer)
        result = state["result"]
        if answer["action"] == "candidate" and answer["relevant"] and result["status"] == "test_failure":
            state["candidate"] = True
            return self.stop(state, "candidate")
        if state["mode"] == "baseline":
            return self.stop(state, "single_pass_complete")
        if answer["action"] == "stop":
            return self.stop(state, "agent_stop")
        if state["test_versions"] >= self.limit(state, "test_versions"):
            return self.stop(state, "test_version_budget")
        if not self.progress(state, digest(state["proposal"]["code"] + result["status"])):
            return self.stop(state, "no_progress")
        state["next_node"] = "search" if answer["action"] == "search" else "generate"
        return self.gate(state)

    def freeze(self, state, answer):
        test, frozen = state["test"], None
        if test is not None:
            frozen = {"path": test["path"], "code": test["code"], "sha256": digest(test["code"]),
                      "expected_failure": test["expected_failure"], "instance_id": self.task["instance_id"],
                      "base_commit": self.task["base_commit"], "source_sha256": self.index["source_sha256"],
                      "runtime": self.runner.runtime}
            frozen["artifact_sha256"] = digest(json.dumps(frozen, sort_keys=True))
            write_json(self.output / "frozen.json", frozen)
        summary = {"instance_id": self.task["instance_id"], "base_commit": self.task["base_commit"],
                   "mode": state["mode"], "model": self.config.model,
                   **{k: state[k] for k in ("candidate", "stop_reason", "error", "search_rounds", "test_versions", "test_runs")},
                   "last_result": state["result"], "frozen_sha256": frozen["sha256"] if frozen else None,
                   **self.budget.summary()}
        write_json(self.output / "summary.json", summary)
        self.events.emit("stop", **summary)
        state.update(frozen=frozen, summary=summary, next_node="end",
                     explanation="Generering avslutad. Utvärdera det frysta testet separat med facit.")
        return state

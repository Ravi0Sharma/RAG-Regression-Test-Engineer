"""Manual exchange, shared workflow parity and restart/interrupt boundaries."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from rag_regression.core import Budget, Config, Events, read_json, write_json
from rag_regression.demo import CORRECT, ScriptedModel, prepare_demo
from rag_regression.evaluation import evaluate, report
from rag_regression.handoff import Handoff
from rag_regression.agent import run_agent
from rag_regression.model import InvalidSourceRequest
from rag_regression.runner import DockerRunner


class RecordingRunner:
    calls = []

    def __init__(self, task, config, runtime=None):
        self.runtime = runtime or {"backend": "unittest", "image": "fixture"}

    def run(self, test, directory, timeout, patch=None):
        self.calls.append({"code": test["code"], "patch": patch, "timeout": timeout})
        correct = test["code"] == CORRECT
        status = "passed" if correct and patch else "test_failure" if correct else "syntax_import_error"
        result = {"status": status, "executed": int(correct), "detail": status,
                  "failed": ["case"] if status == "test_failure" else [],
                  "passed": ["case"] if status == "passed" else [], "skipped": [], "timed_out": False}
        write_json(Path(directory) / "result.json", result)
        return result


class HandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        task, _, selection = prepare_demo(self.root / "fixture")
        write_json(selection, {"ids": ["demo__labels-1"], "split": "dev"})
        write_json(task / "runtime.json", {"backend": "unittest", "image": "fixture"})
        task_data = read_json(task / "task.json")
        task_data["patch"] = "SECRET_REFERENCE_FIX"
        write_json(task / "task.json", task_data)
        self.task = task
        self.store = Handoff(self.root / "runs", self.root / "fixture/tasks", RecordingRunner)
        self.events = Events(self.root / "model-events.jsonl")
        self.model = ScriptedModel(self.events)
        RecordingRunner.calls = []

    def start(self, **kwargs):
        view = self.store.start("demo__labels-1", **kwargs)
        self.run_id = view["run_id"]
        return self.store.advance(self.run_id)

    def response(self, view):
        packet = view["request"]
        self.model.generations = view["test_versions"]
        return {"request_id": packet["request_id"],
                "answer": self.model.ask(packet["stage"], packet["context"]),
                "provenance": {"provider": "test-fixture", "model": "scripted",
                               "manual_edits": False, "note": "Automated fixture, not model evidence"}}

    def finish(self, view):
        while view["request"]:
            response = self.response(view)
            self.store.submit(view["run_id"], response)
            view = self.store.advance(view["run_id"])
        return view

    def test_shared_cli_and_manual_flow_have_same_outcomes(self):
        done = self.finish(self.start())
        config = Config(model="manual-handoff", retrieval="lexical")
        events = Events(self.root / "cli/events.jsonl")
        cli = run_agent(self.task, self.root / "cli", "agentic", config, ScriptedModel(events),
                        Budget(config), events, RecordingRunner({}, config))
        for field in ("candidate", "stop_reason", "search_rounds", "test_versions", "test_runs", "frozen_sha256"):
            self.assertEqual(cli[field], done["summary"][field], field)
        self.assertEqual(done["summary"]["model_requests"], 7)
        self.assertIsNone(done["summary"]["external_usage"]["cost_usd"])
        self.assertTrue(all(call["patch"] is None for call in RecordingRunner.calls))
        run = self.store.directory(done["run_id"]) / "generation"
        result = evaluate(run, self.root / "fixture/tasks/oracle/demo__labels-1.json", runner_factory=RecordingRunner)
        self.assertTrue(result["fail_to_pass"])
        self.assertEqual(result["status"], "pending_review")
        with self.assertRaisesRegex(ValueError, "unmetered"):
            report(self.root / "runs", self.root / "fixture/tasks/selection.json", self.root / "report")

    def test_heldout_selection_requires_explicit_matching_split(self):
        selection = self.root / "fixture/tasks/selection.json"
        write_json(selection, {"ids": ["demo__labels-1"], "split": "test"})
        with self.assertRaisesRegex(ValueError, "dev selection"):
            self.store.start("demo__labels-1")
        store = Handoff(self.root / "test-runs", self.store.dev, RecordingRunner,
                        selection_split="test")
        view = store.start("demo__labels-1")
        packet = store.advance(view["run_id"])["request"]
        self.assertNotIn("SECRET_REFERENCE_FIX", json.dumps(packet))
        session = read_json(store.directory(view["run_id"]) / "session.json")
        self.assertEqual(session["selection_split"], "test")
        with self.assertRaisesRegex(ValueError, "test selection"):
            store.start("not__selected-1")
        write_json(selection, {"ids": ["demo__labels-1"], "split": "dev"})
        with self.assertRaisesRegex(ValueError, "test selection"):
            store.start("demo__labels-1")

    def test_request_uses_allowlist_and_survives_restart(self):
        view = self.start()
        packet = view["request"]
        text = json.dumps(packet)
        self.assertNotIn("SECRET_REFERENCE_FIX", text)
        self.assertNotIn("demo-only-strip-fix", text)
        self.assertNotIn("runtime.json", text)
        self.assertNotIn("api_key", text.lower())
        store = Handoff(self.store.runs, self.store.dev, RecordingRunner)
        self.assertEqual(store.advance(self.run_id)["request"], packet)
        imported = store.submit(self.run_id, self.response(view))
        self.assertTrue(imported["response_imported"])
        self.assertEqual(RecordingRunner.calls, [])
        next_view = store.advance(self.run_id)
        self.assertEqual(next_view["request"]["stage"], "assess")
        self.assertNotEqual(next_view["request"]["request_id"], packet["request_id"])
        with self.assertRaisesRegex(ValueError, "another request"):
            store.submit(self.run_id, self.response(view))

    def test_invalid_responses_and_manual_edits_do_not_advance(self):
        view = self.start()
        response = self.response(view)
        response["answer"]["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            self.store.submit(self.run_id, response)
        response = self.response(view)
        response["answer"]["queries"] = []
        with self.assertRaisesRegex(ValueError, "nonempty"):
            self.store.submit(self.run_id, response)
        response = self.response(view)
        response["provenance"].update(manual_edits=True, note="")
        with self.assertRaisesRegex(ValueError, "corrections"):
            self.store.submit(self.run_id, response)
        self.assertEqual(self.store.status(self.run_id)["revision"], view["revision"])
        response["provenance"]["note"] = "Changed the path filter; original response retained outside this fixture"
        self.store.submit(self.run_id, response)
        saved = read_json(self.store.directory(self.run_id) / "exchange" / (response["request_id"] + ".response.json"))
        self.assertEqual(saved, response)

    def test_replay_does_not_execute_again_and_crash_is_not_retried(self):
        view = self.start()
        while view["request"]["stage"] != "generate":
            self.store.submit(self.run_id, self.response(view))
            view = self.store.advance(self.run_id)
        self.store.submit(self.run_id, self.response(view))
        ready = self.store.step(self.run_id)
        executed = self.store.step(self.run_id, ready["revision"])
        self.assertEqual(len(RecordingRunner.calls), 1)
        replay = self.store.step(self.run_id, ready["revision"])
        self.assertEqual(replay, executed)
        self.assertEqual(len(RecordingRunner.calls), 1)
        with self.assertRaisesRegex(ValueError, "Stale"):
            self.store.step(self.run_id, 0)
        view = self.store.advance(self.run_id)
        self.store.submit(self.run_id, self.response(view))
        write_json(self.store.directory(self.run_id) / "inflight.json", {"stage": "decide"})
        with self.assertRaisesRegex(ValueError, "unresolved"):
            self.store.step(self.run_id)

    def test_docker_preflight_failure_keeps_generated_proposal_for_advance_retry(self):
        failures = [None, RuntimeError("Docker preflight failed: permission denied"), None]
        with patch("rag_regression.handoff.DockerRunner", RecordingRunner), \
             patch("rag_regression.handoff.check_runtime"), \
             patch("rag_regression.handoff.preflight_docker", side_effect=failures):
            store = Handoff(self.root / "docker-runs", self.store.dev, RecordingRunner)
            view = store.start("demo__labels-1")
            run_id = view["run_id"]
            view = store.advance(run_id)
            while view["request"]["stage"] != "generate":
                store.submit(run_id, self.response(view))
                view = store.advance(run_id)
            store.submit(run_id, self.response(view))
            with self.assertRaisesRegex(RuntimeError, "permission denied"):
                store.advance(run_id)

            resumed = store.status(run_id)
            self.assertEqual(resumed["next_node"], "execute")
            self.assertEqual(resumed["test_runs"], 0)
            self.assertEqual(resumed["test_versions"], 1)
            self.assertIsNone(resumed["request"])
            self.assertTrue(resumed["proposal"]["code"])
            self.assertFalse((store.directory(run_id) / "inflight.json").exists())

            next_view = store.advance(run_id)
            self.assertEqual(next_view["next_node"], "decide")
            self.assertEqual(next_view["test_runs"], 1)
            self.assertEqual(len(RecordingRunner.calls), 1)

    def test_docker_runner_error_after_preflight_is_saved_as_execution_evidence(self):
        runtime = read_json(self.task / "runtime.json")
        runtime.update(image="sha256:" + "a" * 64, platform="linux/amd64", python="/python")
        write_json(self.task / "runtime.json", runtime)
        with patch("rag_regression.handoff.check_runtime"), \
             patch("rag_regression.handoff.preflight_docker"), \
             patch("rag_regression.runner.docker_command", side_effect=lambda args, **kwargs:
                   subprocess.CompletedProcess(args, 1, "", "permission denied")):
            store = Handoff(self.root / "docker-runner-runs", self.store.dev, DockerRunner)
            view = store.start("demo__labels-1")
            run_id = view["run_id"]
            view = store.advance(run_id)
            while view["request"]["stage"] != "generate":
                store.submit(run_id, self.response(view))
                view = store.advance(run_id)
            store.submit(run_id, self.response(view))
            decided = store.advance(run_id)

        self.assertEqual(decided["next_node"], "decide")
        self.assertEqual(decided["request"]["stage"], "decide")
        self.assertEqual(decided["test_runs"], 1)
        self.assertEqual(decided["result"]["status"], "environment_error")
        attempt = store.directory(run_id) / "generation/attempt-1"
        self.assertIn("permission denied", (attempt / "output.log").read_text())
        self.assertEqual(read_json(attempt / "result.json")["executed"], 0)
        self.assertFalse((store.directory(run_id) / "inflight.json").exists())

    def test_evaluation_preflight_preserves_destination_and_does_not_read_oracle(self):
        done = self.finish(self.start())
        run = self.store.directory(done["run_id"]) / "generation"
        missing_oracle = self.root / "oracle-not-read.json"
        with patch("rag_regression.evaluation.preflight_docker",
                   side_effect=RuntimeError("Docker preflight failed: permission denied")):
            with self.assertRaisesRegex(RuntimeError, "Docker preflight failed"):
                evaluate(run, missing_oracle)
        self.assertFalse((run / "evaluation").exists())

    def test_input_changes_and_wrong_stage_rejected(self):
        view = self.start()
        with self.assertRaisesRegex(ValueError, "saved workflow stage"):
            self.store.step(self.run_id, view["revision"], expected_stage="execute")
        (self.task / "source/labels.py").write_text("modified")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            self.store.advance(self.run_id)
        with self.assertRaisesRegex(ValueError, "dev selection"):
            self.store.start("../../oracle")

    def test_request_and_search_budgets_and_baseline(self):
        view = self.start(config=Config(max_api_calls=1))
        self.store.submit(self.run_id, self.response(view))
        done = self.store.advance(self.run_id)
        self.assertEqual(done["summary"]["stop_reason"], "model_request_budget")
        self.assertEqual(RecordingRunner.calls, [])
        view = self.start(config=Config(max_search_rounds=1))
        self.store.submit(self.run_id, self.response(view))
        view = self.store.advance(self.run_id)
        reply = self.response(view)
        reply["answer"].update(sufficient=False, missing=["fixture"], missing_categories=["setup"])
        self.store.submit(self.run_id, reply)
        done = self.store.advance(self.run_id)
        self.assertEqual(done["summary"]["stop_reason"], "search_budget")
        done = self.finish(self.start(mode="baseline"))
        self.assertEqual(done["summary"]["test_versions"], 1)
        self.assertFalse(done["candidate"])

    def test_no_followup_queries_required_when_search_budget_is_exhausted(self):
        for mode in ("baseline", "agentic"):
            view = self.start(mode=mode, config=Config(max_search_rounds=1))
            self.store.submit(self.run_id, self.response(view))
            view = self.store.advance(self.run_id)
            self.assertEqual(view["request"]["context"]["remaining_search_rounds"], 0)
            reply = self.response(view)
            reply["answer"].update(sufficient=False, missing=["fixture"], missing_categories=["setup"], queries=[], kinds=[])
            self.store.submit(self.run_id, reply)
            view = self.store.advance(self.run_id)
            if mode == "baseline":
                self.assertEqual(view["request"]["stage"], "generate")
            else:
                self.assertEqual(view["summary"]["stop_reason"], "search_budget")

    def test_assessment_gap_contract_and_context_read_survive_restart(self):
        view = self.start()
        self.store.submit(self.run_id, self.response(view))
        view = self.store.advance(self.run_id)
        reply = self.response(view)
        reply["answer"].update(sufficient=False, missing=["setup"])
        with self.assertRaisesRegex(ValueError, "missing categories"):
            self.store.submit(self.run_id, reply)
        anchor = view["sources"][0]["id"]
        reply["answer"].update(missing_categories=["setup"], read_source_ids=[anchor])
        self.store.submit(self.run_id, reply)
        restarted = Handoff(self.store.runs, self.store.dev, RecordingRunner)
        view = restarted.advance(self.run_id)
        self.assertEqual(view["search_trace"]["mode"], "source_context")
        self.assertEqual(view["search_rounds"], 2)
        self.assertEqual(view["unresolved_rounds"], 1)
        self.assertEqual(view["request"]["stage"], "assess")
        self.assertEqual(RecordingRunner.calls, [])

    def invalid_source_response(self, view):
        response = self.response(view)
        response['answer'].update(sufficient=False, missing=['setup'], missing_categories=['setup'],
                                  read_source_ids=['not-a-supplied-id'])
        return response

    def assessment(self, **kwargs):
        view = self.start(**kwargs)
        self.store.submit(self.run_id, self.response(view))
        return self.store.advance(self.run_id)

    def test_invalid_source_is_preserved_and_correctable_after_restart(self):
        view = self.assessment()
        invalid = self.invalid_source_response(view)
        repaired = self.store.submit(self.run_id, invalid)
        self.assertEqual(repaired['search_rounds'], 1)
        self.assertEqual(repaired['model_requests'], 3)
        self.assertFalse(repaired['response_imported'])
        self.assertNotEqual(repaired['request']['request_id'], view['request']['request_id'])
        self.assertEqual(repaired['request']['context']['sources'], view['request']['context']['sources'])
        self.assertEqual(repaired['request']['context']['previous_invalid_answer'], invalid['answer'])
        exchange = self.store.directory(self.run_id) / 'exchange'
        self.assertEqual(read_json(exchange / (invalid['request_id'] + '.rejected.json'))['response'], invalid)
        restarted = Handoff(self.store.runs, self.store.dev, RecordingRunner)
        self.assertEqual(restarted.advance(self.run_id)['request'], repaired['request'])
        with self.assertRaisesRegex(ValueError, 'another request'):
            restarted.submit(self.run_id, invalid)
        corrected = self.invalid_source_response(repaired)
        corrected['answer']['read_source_ids'] = [repaired['sources'][0]['id']]
        restarted.submit(self.run_id, corrected)
        following = restarted.advance(self.run_id)
        self.assertEqual(following['search_rounds'], 2)
        self.assertEqual(following['search_trace']['mode'], 'source_context')
        self.assertEqual(RecordingRunner.calls, [])

    def test_source_repair_limit_and_total_budget_stop_without_tools(self):
        for calls, attempts, reason in ((24, 2, 'invalid_source_request'), (2, 1, 'model_request_budget')):
            view = self.assessment(config=Config(max_api_calls=calls))
            for _ in range(attempts):
                view = self.store.submit(self.run_id, self.invalid_source_response(view))
            done = self.store.advance(self.run_id)
            self.assertEqual(done['summary']['stop_reason'], reason)
            self.assertEqual(done['search_rounds'], 1)
            self.assertEqual(done['model_requests'], 2 + (attempts - 1))
            self.assertEqual(RecordingRunner.calls, [])

    def test_source_ids_must_be_unique_bounded_and_from_current_packet(self):
        view = self.assessment()
        identifier = view['sources'][0]['id']
        for ids in (['not-a-supplied-id'], [identifier, identifier], [identifier] * 4):
            response = self.invalid_source_response(view)
            response['answer']['read_source_ids'] = ids
            with self.assertRaises(InvalidSourceRequest):
                self.store.validate_response(view['request'], response)

    def test_cli_repairs_source_selection_before_executing_tools(self):
        config = Config(retrieval='lexical')
        events = Events(self.root / 'cli-repair/events.jsonl')
        model = ScriptedModel(events)
        original = model.ask
        repairs = []
        bad = False
        def ask(stage, context):
            nonlocal bad
            answer = original(stage, context)
            if stage == 'assess' and not bad:
                bad = True
                answer.update(sufficient=False, missing=['setup'], missing_categories=['setup'],
                              read_source_ids=['not-a-supplied-id'])
            if 'validation_error' in context:
                repairs.append(context)
            return answer
        model.ask = ask
        result = run_agent(self.task, self.root / 'cli-repair', 'agentic', config, model,
                           Budget(config), events, RecordingRunner({}, config))
        self.assertTrue(result['candidate'])
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]['previous_invalid_answer']['read_source_ids'], ['not-a-supplied-id'])
        self.assertEqual(result['search_rounds'], 2)

    def test_cli_source_repair_stops_at_retry_or_api_budget(self):
        for calls, expected in ((24, 'invalid_source_request'), (2, 'api_call_budget')):
            config = Config(retrieval='lexical', max_api_calls=calls)
            budget = Budget(config)
            output = self.root / ('cli-limit-%d' % calls)
            events = Events(output / 'events.jsonl')
            model = ScriptedModel(events)
            original = model.ask
            def ask(stage, context):
                budget.check(api=True)
                budget.api_calls += 1
                answer = original(stage, context)
                if stage == 'assess':
                    answer.update(sufficient=False, missing=['setup'], missing_categories=['setup'],
                                  read_source_ids=['not-a-supplied-id'])
                return answer
            model.ask = ask
            result = run_agent(self.task, output, 'agentic', config, model, budget, events,
                               RecordingRunner({}, config))
            self.assertEqual(result['stop_reason'], expected)
            self.assertEqual(result['search_rounds'], 1)
            self.assertEqual(budget.api_calls, min(calls, 3))
            self.assertEqual(RecordingRunner.calls, [])

    @unittest.skipUnless(importlib.util.find_spec('langgraph'), 'Install .[studio]')
    def test_studio_refreshes_external_repair_and_stops_after_second_invalid_id(self):
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.types import Command
        from langsmith import tracing_context
        from rag_regression.handoff_graph import build_handoff_graph
        graph = build_handoff_graph(self.store, InMemorySaver())
        config = {'configurable': {'thread_id': 'repair-limit'}, 'recursion_limit': 100}
        with tracing_context(enabled=False):
            view = graph.invoke({'instance_id': 'demo__labels-1'}, config)
            self.run_id = view['run_id']
            view = graph.invoke(Command(resume=self.response(self.store.status(self.run_id))), config)
            old = self.store.status(self.run_id)
            corrected = self.store.submit(self.run_id, self.invalid_source_response(old))
            view = graph.invoke(Command(resume={'request_id': old['request']['request_id']}), config)
            self.assertEqual(view['__interrupt__'][0].value['request_id'], corrected['request']['request_id'])
            view = graph.invoke(Command(resume=self.invalid_source_response(corrected)), config)
        self.assertEqual(view['next_node'], 'end')
        self.assertEqual(view['stop_reason'], 'invalid_source_request')
        self.assertEqual(RecordingRunner.calls, [])

    def test_wait_is_excluded_but_active_time_budget_is_enforced(self):
        view = self.start()
        path = self.store.directory(self.run_id) / "session.json"
        session = read_json(path)
        session["created"] -= 86400
        write_json(path, session)
        self.assertEqual(self.store.advance(self.run_id)["request"], view["request"])
        session = read_json(path)
        session["active_seconds"] = 601
        write_json(path, session)
        done = self.store.advance(self.run_id)
        self.assertEqual(done["summary"]["stop_reason"], "time_budget")
        self.assertEqual(done["next_node"], "end")

    @unittest.skipUnless(importlib.util.find_spec("langgraph"), "Install .[studio]")
    def test_graph_interrupt_rejects_bad_response_and_ignores_edited_state(self):
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.types import Command
        from langsmith import tracing_context
        from rag_regression.handoff_graph import build_handoff_graph
        graph = build_handoff_graph(self.store, InMemorySaver())
        config = {"configurable": {"thread_id": "test"}, "recursion_limit": 100}
        with tracing_context(enabled=False):
            view = graph.invoke({"instance_id": "demo__labels-1"}, config)
            self.assertTrue(view["__interrupt__"])
            self.run_id = view["run_id"]
            self.assertEqual(view["test_runs"], 0)
            bad = graph.invoke(Command(resume={"request_id": "wrong"}), config)
            self.assertIn("matching response", bad["__interrupt__"][0].value["validation_error"])
            view = graph.invoke(Command(resume=self.response(self.store.status(self.run_id))), config)
            self.assertEqual(view["__interrupt__"][0].value["stage"], "assess")
            old_request = self.store.status(self.run_id)['request']['request_id']
            view = graph.invoke(Command(resume=self.invalid_source_response(self.store.status(self.run_id))), config)
            self.assertNotEqual(view['__interrupt__'][0].value['request_id'], old_request)
            self.assertIn('read_source_ids', view['__interrupt__'][0].value['validation_error'])
            while view.get("__interrupt__"):
                canonical = self.store.status(self.run_id)
                if canonical["request"]["stage"] == "decide":
                    graph.update_state(config, {"result": {"status": "passed"}, "proposal": {"code": "malicious"}})
                view = graph.invoke(Command(resume=self.response(canonical)), config)
        self.assertEqual(view["next_node"], "end")
        self.assertTrue(view["candidate"])
        self.assertEqual(len(RecordingRunner.calls), 2)
        self.assertTrue(all(call["code"] != "malicious" for call in RecordingRunner.calls))

    @unittest.skipUnless(importlib.util.find_spec("langgraph"), "Install .[studio]")
    def test_graph_loads_by_file_path_like_the_studio_server(self):
        path = Path(__file__).resolve().parent.parent / "rag_regression/handoff_graph.py"
        spec = importlib.util.spec_from_file_location("standalone_handoff", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIn("execute", module.graph.nodes)
        self.assertIn("evaluate_frozen_test", module.evaluation_graph.nodes)


if __name__ == "__main__":
    unittest.main()

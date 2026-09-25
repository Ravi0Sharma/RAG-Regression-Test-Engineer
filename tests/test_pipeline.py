import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_regression.agent import run_agent
from rag_regression.core import Budget, BudgetExceeded, Config, Events, digest, read_json, write_json
from rag_regression.data import extract_snapshot, import_tasks, public_task
from rag_regression.demo import BUG, CORRECT, DemoRunner, ScriptedModel, demo
from rag_regression.evaluation import evaluate, report, review, verify_frozen
from rag_regression.model import OpenAIModel, PLAN, validate_schema
from rag_regression.retrieval import Retriever, build_index, chunks_for_file, rrf
from rag_regression.runner import DockerRunner, classify, validate_test


def row(identifier="demo__labels-1"):
    return {"instance_id": identifier, "repo": "demo/labels", "base_commit": "a"*40,
            "version": "1", "problem_statement": "Strip spaces when normalizing labels",
            "patch": "SECRET_GOLD", "test_patch": "SECRET_TEST", "hints_text": "SECRET_HINT"}


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def task(self):
        directory = self.root / "task"
        source = directory / "source"
        source.mkdir(parents=True)
        (source / "labels.py").write_text(BUG)
        (source / "test_labels.py").write_text("from labels import normalize\n")
        write_json(directory / "task.json", row())  # Deliberately includes forbidden extras.
        config = Config(retrieval="lexical")
        build_index(directory, config)
        return directory, config


class DataTests(Workspace):
    def test_allowlist_removes_all_oracle_fields(self):
        self.assertNotIn("SECRET", json.dumps(public_task(row())))

    def test_seeded_selection_and_oracle_separation(self):
        rows = [row("demo__labels-" + str(n)) for n in range(6)]
        first = import_tasks(rows, self.root / "a", 3)
        second = import_tasks(list(reversed(rows)), self.root / "b", 3)
        self.assertEqual(first, second)
        for path in (self.root / "a/public").rglob("*.json"):
            self.assertNotIn("SECRET", path.read_text())
        with self.assertRaises(ValueError):
            import_tasks(rows, self.root / "a", 3)

    def test_traversal_rejected_and_symlink_not_extracted(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            member = tarfile.TarInfo("../outside")
            member.size = 3
            archive.addfile(member, io.BytesIO(b"bad"))
        with self.assertRaises(ValueError):
            extract_snapshot(data.getvalue(), self.root / "source")
        self.assertFalse((self.root / "outside").exists())
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            member = tarfile.TarInfo("link")
            member.type, member.linkname = tarfile.SYMTYPE, "/etc/passwd"
            archive.addfile(member)
        extract_snapshot(data.getvalue(), self.root / "safe")
        self.assertFalse((self.root / "safe/link").exists())


class RetrievalTests(Workspace):
    def test_changed_snapshot_rejected_before_indexing(self):
        directory, config = self.task()
        write_json(directory / "snapshot.json", {"base_commit": "a"*40, "source_sha256": "wrong"})
        with self.assertRaisesRegex(ValueError, "snapshot"):
            build_index(directory, config)

    def test_symbols_provenance_and_line_windows(self):
        text = "import os\n\n@decorator\ndef normalize(value):\n    return value\n\nclass Label:\n    pass\n"
        chunks = list(chunks_for_file("labels.py", text, row(), 3))
        symbol = next(c for c in chunks if c["symbol"] == "normalize")
        self.assertEqual(symbol["start"], 3)
        self.assertEqual(symbol["commit"], "a"*40)
        self.assertIn("@decorator", symbol["text"])

    def test_fusion_rewards_shared_hits_without_duplicates(self):
        ranking, scores = rrf([["a", "b", "b"], ["b", "c"]])
        self.assertEqual(ranking[0], "b")
        self.assertAlmostEqual(scores["b"], 1/62 + 1/61)

    def test_routing_and_commit_validation(self):
        directory, config = self.task()
        index = read_json(directory / "index.json")
        retriever = Retriever(index, row(), config)
        hits, trace = retriever.search(["normalize", "labels normalize"], ["tests"])
        self.assertTrue(hits)
        self.assertTrue(all(h["kind"] == "tests" for h in hits))
        self.assertEqual(len(trace["queries"]), 2)
        self.assertEqual(retriever.search(["normalize"], ["code"], "missing/*")[0], [])
        with self.assertRaises(ValueError):
            Retriever(index, {**row(), "base_commit": "b"*40}, config)

    def test_hybrid_uses_embeddings_and_checks_model(self):
        directory, config = self.task()
        config.retrieval = "hybrid"
        index = build_index(directory, config, lambda texts: [[1., 0.] for _ in texts])
        search = Retriever(index, row(), config, lambda texts: [[1., 0.] for _ in texts])
        self.assertTrue(search.search(["synonym absent from code"], ["code"])[0])
        config.embedding_model = "different-model"
        with self.assertRaises(ValueError):
            Retriever(index, row(), config)


class ExecutionTests(Workspace):
    def test_unittest_setup_error_is_not_a_bug_failure(self):
        import rag_regression.runtime as runtime
        path = self.root / "test_example.py"
        path.write_text("import unittest\nclass Example(unittest.TestCase):\n    def setUp(self):\n        raise ValueError('fixture broken')\n    def test_one(self):\n        assert True\n")
        execution = self.root / "execution.json"
        process = subprocess.run([sys.executable, runtime.__file__, "unittest", str(path), str(execution)],
                                 capture_output=True, text=True, timeout=20)
        self.assertEqual(classify(read_json(execution), process.returncode, process.stdout)["status"], "test_error")

    def test_timeout_kills_container_and_preserves_log(self):
        runtime = {"backend": "pytest", "image": "sha256:" + "a"*64,
                   "platform": "linux/amd64", "python": "/python"}
        commands = []
        def docker(arguments, **kwargs):
            commands.append(arguments)
            if arguments[0] == "start":
                raise subprocess.TimeoutExpired(arguments, 1, output="last log")
            return subprocess.CompletedProcess(arguments, 0, "", "")
        with patch("rag_regression.runner.docker_command", side_effect=docker):
            result = DockerRunner(row(), Config(), runtime).run(
                {"path": "test_rag_regression.py", "code": CORRECT}, self.root / "attempt", 1)
        self.assertEqual(result["status"], "environment_error")
        self.assertTrue(result["timed_out"])
        self.assertEqual(commands[-1][:2], ["rm", "-f"])
        self.assertIn("--network", commands[0])
        self.assertIn("last log", (self.root / "attempt/output.log").read_text())

    def case(self, outcome="passed", phase="call", **extra):
        return {"id": "test_one", "phase": phase, "outcome": outcome, "detail": "", **extra}

    def test_missing_or_skipped_tests_never_pass(self):
        for report in (None, {"cases": []}, {"cases": [self.case("skipped")]}):
            self.assertNotEqual(classify(report, 0, "passed")["status"], "passed")

    def test_fixture_error_is_not_bug_failure(self):
        report = {"cases": [self.case("failed", "setup", detail="fixture missing")]}
        self.assertEqual(classify(report, 1, "")["status"], "test_error")

    def test_import_and_assertion_distinguished(self):
        imports = {"collection_errors": ["ModuleNotFoundError: labels"]}
        assertion = {"cases": [self.case("failed", detail="AssertionError: spaces remain")]}
        self.assertEqual(classify(imports, 2, "")["status"], "syntax_import_error")
        self.assertEqual(classify(assertion, 1, "")["status"], "test_failure")
        self.assertEqual(classify(assertion, 1, "", timed_out=True)["status"], "environment_error")

    def test_generated_file_cannot_overwrite_source_or_escape(self):
        directory, config = self.task()
        for path in ("labels.py", "../test_rag_regression.py", "/test_rag_regression.py", "missing/test_rag_regression.py"):
            with self.assertRaises(ValueError):
                validate_test({"path": path, "code": CORRECT}, directory / "source")
        validate_test({"path": "test_rag_regression.py", "code": CORRECT}, directory / "source")

    def test_runtime_requires_pinned_image(self):
        with self.assertRaises(ValueError):
            DockerRunner(row(), Config(), {"backend": "pytest", "image": "moving:latest"})

    @unittest.skipUnless(__import__('importlib').util.find_spec('pytest'), 'pytest optional for runtime integration')
    def test_real_pytest_records_pass_failure_skip_and_fixture_error(self):
        import rag_regression.runtime as runtime
        samples = [
            ("def test_one():\n    assert True\n", "passed"),
            ("def test_one():\n    assert False\n", "test_failure"),
            ("import pytest\n@pytest.mark.skip(reason='demo')\ndef test_one():\n    pass\n", "not_executed"),
            ("def test_one(missing_fixture):\n    pass\n", "test_error"),
            ("from no_such_module_rag import missing\n", "syntax_import_error"),
        ]
        for number, (code, expected) in enumerate(samples):
            with self.subTest(expected=expected):
                working = self.root / str(number)
                working.mkdir()
                (working / "test_example.py").write_text(code)
                execution = working / "execution.json"
                process = subprocess.run([sys.executable, runtime.__file__, "pytest", "test_example.py", str(execution)],
                                         cwd=working, capture_output=True, text=True, timeout=20)
                self.assertEqual(classify(read_json(execution), process.returncode, process.stdout)["status"], expected)


class ModelTests(Workspace):
    def test_time_budget_stops_before_another_call(self):
        budget = Budget(Config(max_seconds=1))
        budget.started -= 2
        with self.assertRaisesRegex(BudgetExceeded, "time_budget"):
            budget.check()

    def test_invalid_configuration_types_rejected(self):
        for value in ("600", True, -1, float('nan')):
            with self.assertRaises(ValueError):
                Config(max_seconds=value).validate()
    def test_cost_and_token_limits_before_request(self):
        config = Config(max_cost_usd=.001, max_tokens=10)
        budget = Budget(config)
        with self.assertRaises(BudgetExceeded):
            budget.check(reserve_cost=.002)
        with self.assertRaises(BudgetExceeded):
            budget.check(reserve_tokens=11)
        budget.record({"input_tokens": 2, "output_tokens": 3})
        self.assertEqual(budget.input_tokens, 2)

    def test_missing_key_has_clear_error(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
            OpenAIModel(Config(), Budget(Config()), Events(self.root / "events.jsonl"))

    def test_strict_schema_rejects_extra_fields(self):
        with self.assertRaises(ValueError):
            validate_schema({"unexpected": "value"}, PLAN)

    def test_responses_request_and_usage_without_network(self):
        answer = {"needs": [], "queries": ["normalize"], "kinds": ["code"], "path_glob": "*", "reason": "Locate function"}
        response = {"id": "mock", "status": "completed", "usage": {"input_tokens": 20, "output_tokens": 30},
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}]}
        config = Config()
        budget = Budget(config)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret"}), patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as post:
            model = OpenAIModel(config, budget, Events(self.root / "events.jsonl"))
            self.assertEqual(model.ask("plan", {"task": public_task(row())}), answer)
        payload = json.loads(post.call_args.args[0].data)
        self.assertFalse(payload["store"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertNotIn("test-secret", (self.root / "events.jsonl").read_text())
        self.assertEqual(budget.output_tokens, 30)


class AgentTests(Workspace):
    def test_frozen_path_tampering_rejected(self):
        demo(self.root / "demo")
        path = self.root / "demo/runs/agentic/demo__labels-1/frozen.json"
        frozen = read_json(path)
        frozen["path"] = "labels.py"
        with self.assertRaisesRegex(ValueError, "artifact modified"):
            verify_frozen(frozen)

    def test_report_rejects_mixed_models(self):
        demo(self.root / "demo")
        path = self.root / "demo/runs/agentic/demo__labels-1/config.json"
        config = read_json(path)
        config["model"] = "other-model"
        write_json(path, config)
        with self.assertRaisesRegex(ValueError, "mixes models"):
            report(self.root / "demo/runs", self.root / "demo/tasks/selection.json", self.root / "report")

    def test_end_to_end_feedback_and_frozen_before_after(self):
        totals = demo(self.root / "demo")
        self.assertEqual(totals["baseline"]["confirmed_success"], 0)
        self.assertEqual(totals["agentic"]["confirmed_success"], 1)
        run = self.root / "demo/runs/agentic/demo__labels-1"
        summary = read_json(run / "summary.json")
        self.assertEqual(summary["test_versions"], 2)
        self.assertEqual(summary["search_rounds"], 2)
        evaluation = read_json(run / "evaluation/result.json")
        self.assertEqual(evaluation["before"]["status"], "test_failure")
        self.assertEqual(evaluation["after"]["status"], "passed")
        self.assertEqual(evaluation["sha256"], digest(CORRECT))

    def test_public_context_cannot_leak_oracle(self):
        directory, config = self.task()
        events = Events(self.root / "run/events.jsonl")
        model = ScriptedModel(events)
        original = model.ask
        def check(stage, context):
            self.assertNotIn("SECRET", json.dumps(context))
            return original(stage, context)
        model.ask = check
        run_agent(directory, self.root / "run", "agentic", config, model, Budget(config), events, DemoRunner(row(), config))

    def test_insufficient_retrieval_stops_with_budget(self):
        directory, config = self.task()
        config.max_search_rounds = 1
        events = Events(self.root / "run/events.jsonl")
        model = ScriptedModel(events)
        original = model.ask
        def insufficient(stage, context):
            answer = original(stage, context)
            if stage == "assess":
                answer.update(sufficient=False, missing=["fixture"])
            return answer
        model.ask = insufficient
        summary = run_agent(directory, self.root / "run", "agentic", config, model, Budget(config), events, DemoRunner(row(), config))
        self.assertEqual(summary["stop_reason"], "search_budget")
        self.assertEqual(summary["test_versions"], 0)

    def test_source_tampering_rejected_before_model_call(self):
        directory, config = self.task()
        (directory / "source/labels.py").write_text("fixed source")
        events = Events(self.root / "run/events.jsonl")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            run_agent(directory, self.root / "run", "agentic", config, None, Budget(config), events, DemoRunner(row(), config))

    def test_review_cannot_turn_import_error_into_success(self):
        demo(self.root / "demo")
        with self.assertRaisesRegex(ValueError, "fail-to-pass"):
            review(self.root / "demo/runs/baseline/demo__labels-1", "relevant", "Claim")

    def test_missing_runs_remain_in_report_denominator(self):
        write_json(self.root / "selection.json", {"ids": ["a", "b"]})
        totals = report(self.root / "missing", self.root / "selection.json", self.root / "report")
        self.assertEqual(totals["agentic"]["selected"], 2)
        self.assertEqual(totals["agentic"]["success_rate_all_selected"], 0)


if __name__ == "__main__":
    unittest.main()

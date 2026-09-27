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
from rag_regression.workflow import Workflow, search_signature


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
    def test_blank_class_fragments_never_displace_imports_and_setup(self):
        directory, config = self.task()
        text = "import unittest\n\nclass ManyTests(unittest.TestCase):\n"
        text += "".join("\n    def test_%d(self):\n        pass\n" % i for i in range(24))
        text += "\n    def setUp(self):\n        self.value = 1\n"
        (directory / "source/test_many.py").write_text(text)
        index = build_index(directory, config)
        self.assertTrue(any(not c["text"].strip() for c in index["chunks"]))
        retriever = Retriever(index, row(), config)
        anchor = next(c for c in retriever.chunks.values()
                      if c["symbol"] == "ManyTests" and c["text"].strip())
        hits, trace = retriever.read_context([anchor["id"]], [anchor], limit=4)
        self.assertTrue(all(h["text"].strip() for h in hits))
        self.assertIn("ManyTests.setUp", {h["symbol"] for h in hits})
        self.assertIn("<module>", {h["symbol"] for h in hits})
        self.assertTrue(trace["context_truncated"])
        self.assertTrue(all(h["text"].strip() for h in retriever.search(["ManyTests"], ["tests"])[0]))

    def test_context_read_includes_class_setup_and_rejects_unknown_ids(self):
        directory, config = self.task()
        (directory / "source/test_api.py").write_text(
            "import unittest\nfrom labels import normalize\n"
            "class Labels(unittest.TestCase):\n"
            "    def setUp(self):\n        self.value = ' x '\n"
            "    def helper(self):\n        return normalize(self.value)\n"
            "    def test_label(self):\n        self.assertEqual(self.helper(), 'x')\n"
            "def unrelated():\n    return 'secret noise'\n")
        index = build_index(directory, config)
        retriever = Retriever(index, row(), config)
        anchor = next(c for c in index["chunks"] if c["symbol"] == "Labels.test_label")
        hits, trace = retriever.read_context([anchor["id"]], [anchor])
        self.assertEqual({h["symbol"] for h in hits},
                         {"Labels.test_label", "Labels", "Labels.setUp", "Labels.helper", "<module>"})
        self.assertFalse(trace["context_truncated"])
        self.assertEqual(trace["mode"], "source_context")
        self.assertTrue(all(h["commit"] == row()["base_commit"] for h in hits))
        with self.assertRaisesRegex(ValueError, "supplied sources"):
            retriever.read_context(["../../oracle/secret"], [anchor])
        other = next(c for c in index["chunks"] if c["symbol"] == "unrelated")
        with self.assertRaises(ValueError):
            retriever.read_context([other["id"]], [anchor])
        with self.assertRaises(ValueError):
            retriever.read_context([anchor["id"]], [])

    def test_context_distinguishes_class_definition_and_resolves_one_local_base(self):
        directory, config = self.task()
        config.chunk_lines = 4
        package = directory / "source/pkg"
        package.mkdir()
        (package / "test_base.py").write_text("class Base:\n    def setUp(self):\n        self.ready = True\n")
        (package / "test_api.py").write_text(
            "from .test_base import Base\n"
            "class ApiTests(Base):\n"
            "    # setup note\n"
            "    # class ApiTests comment, not a declaration\n"
            "    def helper(self):\n        return self.ready\n"
            "    # class ApiTests still only a comment\n"
            "    def test_api(self):\n        self.assertTrue(self.helper())\n")
        index = build_index(directory, config)
        retriever = Retriever(index, row(), config)
        anchor = next(c for c in index["chunks"] if c["symbol"] == "ApiTests.test_api")
        context_ids = retriever.context_ids(anchor["id"])
        context = [retriever.chunks[key] for key in context_ids]
        comment = next(c for c in index["chunks"] if c["symbol"] == "ApiTests"
                       and "class ApiTests still only a comment" in c["text"])
        classes = [c for c in context if c["symbol"] == "ApiTests"]
        self.assertEqual(len(classes), 1)
        self.assertIn("class ApiTests(Base)", classes[0]["text"])
        self.assertNotIn(comment["id"], context_ids)
        self.assertEqual(sum(c["symbol"] == "Base" for c in context), 1)
        self.assertTrue(any("from .test_base import Base" in c["text"] for c in context))
        config.top_k = 2
        hits, trace = retriever.read_context([anchor["id"]], [anchor], limit=4)
        self.assertTrue(any(c["symbol"] == "Base" for c in hits))
        self.assertTrue(any(c["symbol"] == "ApiTests" for c in hits))
        self.assertTrue(trace["context_truncated"])

    def test_local_base_resolution_skips_missing_and_ambiguous_imports(self):
        directory, config = self.task()
        source = directory / "source/pkg"
        source.mkdir()
        (directory / "source/test_base.py").write_text("class Base:\n    pass\n")
        for name, files in {
                "missing": {"test_api.py": "from .missing import Base\n"},
                "duplicate_class": {"test_base.py": "class Base:\n    pass\nclass Base:\n    pass\n",
                                    "test_api.py": "from .test_base import Base\n"},
                "ambiguous_import": {"test_base.py": "class Base:\n    pass\n",
                                     "other_base.py": "class Base:\n    pass\n",
                                     "test_api.py": "from .test_base import Base\nfrom .other_base import Base\n"},
                "rebound": {"test_base.py": "class Base:\n    pass\n",
                            "test_api.py": "from .test_base import Base\nBase = Other\n"},
                "qualified_base": {"test_base.py": "class Base:\n    pass\n",
                                   "other_base.py": "class Base:\n    pass\n",
                                   "test_api.py": "from .other_base import Base\nclass ApiTests(mod.Base):\n    def test_api(self):\n        pass\n"},
                "beyond_package_root": {"test_api.py": "from ..test_base import Base\n"},
                "package_import": {"test_api.py": "from . import Base\n"},
        }.items():
            with self.subTest(name=name):
                for path in source.rglob("*.py"):
                    path.unlink()
                for filename, text in files.items():
                    (source / filename).write_text(text)
                api = source / "test_api.py"
                if "class ApiTests" not in api.read_text():
                    api.write_text(api.read_text() + "class ApiTests(Base):\n    def test_api(self):\n        pass\n")
                index = build_index(directory, config)
                retriever = Retriever(index, row(), config)
                anchor = next(c for c in index["chunks"] if c["symbol"] == "ApiTests.test_api")
                context = [retriever.chunks[key] for key in retriever.context_ids(anchor["id"])]
                self.assertFalse(any(c["path"].endswith(("test_base.py", "other_base.py")) for c in context))

    def test_context_read_expands_class_body_and_reports_truncation(self):
        directory, config = self.task()
        (directory / "source/test_form.py").write_text(
            "class Form:\n    class Meta:\n        fields = ['name']\n"
            "    def helper(self):\n        return 1\n"
            "    def another(self):\n        return 2\n")
        index = build_index(directory, config)
        anchor = next(c for c in index["chunks"] if c["symbol"] == "Form")
        retriever = Retriever(index, row(), config)
        hits, trace = retriever.read_context([anchor["id"]], [anchor])
        self.assertTrue(any(h["symbol"] == "Form.Meta" and 'fields' in h["text"] for h in hits))
        config.top_k = 1
        hits, trace = retriever.read_context([anchor["id"]], [anchor])
        self.assertEqual(len(hits), 2)
        self.assertTrue(trace["context_truncated"])
        anchors = index["chunks"][:3]
        hits, trace = retriever.read_context([c["id"] for c in anchors], anchors)
        self.assertEqual(trace["selected_ids"], [h["id"] for h in hits])
        self.assertEqual(len(trace["requested_ids"]), 3)

    def test_python_test_patterns_survive_fixture_crowding_with_import_context(self):
        directory, config = self.task()
        config.top_k = 4
        source = directory / "source"
        fixtures = source / "tests/fixtures"
        fixtures.mkdir(parents=True)
        for n in range(12):
            (fixtures / (str(n) + ".sql")).write_text("normalize labels spaces\n")
        text = ("from labels import normalize\n\n"
                "def test_normalize():\n    assert normalize(' x ') == 'x'\n")
        (source / "tests/test_api.py").write_text(text)
        index = build_index(directory, config)
        retriever = Retriever(index, row(), config)
        hits, trace = retriever.search(["normalize labels spaces"], ["tests"])
        pattern = next(h for h in hits if h["symbol"] == "test_normalize")
        self.assertIn(pattern["id"], trace["test_pattern_ids"])
        self.assertNotIn(pattern["id"], [r["id"] for r in trace["fusion"]])
        context = next(h for h in hits if h["id"] in trace["context_ids"])
        self.assertEqual(context["path"], pattern["path"])
        self.assertEqual(context["text"], "\n".join(text.splitlines()[context["start"]-1:context["end"]]))
        self.assertIn("from labels import normalize", context["text"])
        self.assertEqual(len(trace["selected_ids"]), config.top_k)
        self.assertLessEqual(len(hits), 2 * config.top_k)
        self.assertEqual(len({h["id"] for h in hits}), len(hits))
        self.assertTrue(all(h["repo"] == row()["repo"] and h["commit"] == row()["base_commit"] for h in hits))
        # Context expansion must never escape the model's kind/path filter or
        # manufacture test evidence for an unmatched lexical query.
        for kinds, glob in [(["code"], "*"), (["tests"], "tests/fixtures/*")]:
            restricted, trace = retriever.search(["normalize labels spaces"], kinds, glob)
            self.assertFalse(trace["test_pattern_ids"])
            self.assertFalse(trace["context_ids"])
            self.assertTrue(all(h["kind"] in kinds for h in restricted))
        self.assertEqual(retriever.search(["zzzzunmatched"], ["tests"])[0], [])

    def test_hybrid_test_patterns_reuse_query_embeddings(self):
        directory, config = self.task()
        (directory / "source/test_api.py").write_text("from labels import normalize\ndef test_api():\n    assert normalize('a') == 'a'\n")
        config.retrieval = "hybrid"
        index = build_index(directory, config, lambda texts: [[1., 0.] for _ in texts])
        embed = unittest.mock.Mock(return_value=[[1., 0.]])
        hits, trace = Retriever(index, row(), config, embed).search(["semantic synonym"], ["tests"])
        embed.assert_called_once_with(["semantic synonym"])
        self.assertTrue(trace["test_pattern_ids"])
        self.assertTrue(any(h["symbol"] == "test_api" for h in hits))

    def test_test_pattern_includes_referenced_local_helper_without_unrelated_code(self):
        directory, config = self.task()
        (directory / "source/test_api.py").write_text(
            "from labels import normalize\n"
            "def run_check(value):\n    return normalize(value)\n"
            "def unrelated():\n    return 123\n"
            "def test_stripping():\n    assert run_check(' x ') == 'x'\n")
        index = build_index(directory, config)
        hits, trace = Retriever(index, row(), config).search(["stripping"], ["tests"])
        context = [h for h in hits if h["id"] in trace["context_ids"]]
        self.assertEqual({h["symbol"] for h in context}, {"<module>", "run_check"})
        self.assertNotIn("unrelated", [h["symbol"] for h in hits])

    def test_yaml_and_sql_fixtures_are_retrievable_with_provenance(self):
        directory, config = self.task()
        fixtures = directory / "source/test/fixtures"
        fixtures.mkdir(parents=True)
        (fixtures / "L036.yml").write_text("test_cte:\n  fail_str: SELECT my_id, other_thing\n")
        (fixtures / "cte.sql").write_text("WITH example AS (SELECT my_id, other_thing) SELECT * FROM example\n")
        index = build_index(directory, config)
        hits, _ = Retriever(index, row(), config).search(["L036 my_id", "WITH example"], ["tests"])
        self.assertEqual({h["path"] for h in hits if h["path"].endswith((".yml", ".sql"))},
                         {"test/fixtures/L036.yml", "test/fixtures/cte.sql"})
        self.assertTrue(all(h["commit"] == row()["base_commit"] for h in hits))

    def test_class_methods_have_independent_symbols_and_exact_lines(self):
        text = "class Converter:\n    kind = 'SQ'\n    @staticmethod\n    def from_json(value):\n        return value\n\n    def unrelated(self):\n        return None\n"
        chunks = list(chunks_for_file("converter.py", text, row(), 100))
        method = next(c for c in chunks if c["symbol"] == "Converter.from_json")
        self.assertEqual((method["start"], method["end"]), (3, 5))
        self.assertNotIn("unrelated", method["text"])
        covered = [n for c in chunks for n in range(c["start"], c["end"] + 1)]
        self.assertEqual(covered, list(range(1, 9)))
        self.assertEqual(method["text"], "\n".join(text.splitlines()[2:5]))

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
    def test_runtime_check_rejects_missing_backend_and_preserves_evidence(self):
        from rag_regression.runner import check_runtime
        runtime = {"backend": "pytest", "image": "sha256:" + "a" * 64,
                   "platform": "linux/amd64", "python": "/python"}
        output = self.root / "runtime-check.json"
        with patch("rag_regression.runner.docker_command", return_value=
                   subprocess.CompletedProcess([], 1, "", "ModuleNotFoundError: pytest")) as docker:
            with self.assertRaisesRegex(RuntimeError, "backend unavailable"):
                check_runtime(runtime, output)
        self.assertFalse(read_json(output)["ready"])
        self.assertIn("pytest", read_json(output)["stderr"])
        command = docker.call_args_list[0].args[0]
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertEqual(docker.call_args_list[-1].args[0][:2], ["rm", "-f"])

    def test_runtime_check_accepts_imported_backend(self):
        from rag_regression.runner import check_runtime
        runtime = {"backend": "django", "image": "sha256:" + "a" * 64,
                   "platform": "linux/amd64", "python": "/python"}
        with patch("rag_regression.runner.docker_command", return_value=
                   subprocess.CompletedProcess([], 0, '{"module":"django","version":"3.2"}', "")):
            result = check_runtime(runtime, self.root / "runtime-check.json")
        self.assertTrue(result["ready"])
        self.assertEqual(result["versions"]["module"], "django")

    def test_failed_metadata_preparation_stops_before_patch_or_test_execution(self):
        runtime = {"backend": "pytest", "image": "sha256:" + "a"*64,
                   "platform": "linux/amd64", "python": "/python", "refresh_package_metadata": True}
        commands = []
        def docker(arguments, **kwargs):
            commands.append(arguments)
            if arguments[-2:] == ["rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(arguments, 0, row()["base_commit"], "")
            if arguments[-2:] == ["setup.py", "egg_info"]:
                return subprocess.CompletedProcess(arguments, 1, "", "metadata failed")
            return subprocess.CompletedProcess(arguments, 0, "", "")
        with patch("rag_regression.runner.docker_command", side_effect=docker):
            result = DockerRunner(row(), Config(), runtime).run(
                {"path": "test_rag_regression.py", "code": CORRECT}, self.root / "attempt", 30, patch="fix")
        self.assertEqual(result["status"], "environment_error")
        self.assertEqual(result["executed"], 0)
        self.assertIn("metadata failed", (self.root / "attempt/output.log").read_text())
        self.assertFalse(any("apply" in command or "/tmp/rag-runtime.py" in command for command in commands))
        self.assertEqual(commands[-1][:2], ["rm", "-f"])

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
    def workflow(self, config=None):
        directory, default = self.task()
        config = config or default
        return Workflow(directory, self.root / "run", config, Budget(config),
                        Events(self.root / "run/events.jsonl"), DemoRunner(row(), config))

    def test_filename_normalization_preserves_code_and_never_overwrites_source(self):
        flow = self.workflow()
        state = flow.prepare("baseline")
        answer = {"path": "test_labels.py", "code": CORRECT, "expected_failure": "spaces", "reason": "example"}
        state = flow.generate(state, answer)
        self.assertEqual(state["proposal"]["path"], "test_rag_regression.py")
        self.assertEqual(state["proposal"]["code"], CORRECT)
        self.assertEqual(answer["path"], "test_labels.py")
        self.assertIn("test_path_normalized", (self.root / "run/events.jsonl").read_text())
        validate_test(state["proposal"], flow.directory / "source")
        with self.assertRaises(ValueError):
            flow.generate(state, {**answer, "path": "../test_labels.py"})
        (flow.directory / "source/test_rag_regression.py").write_text("existing")
        with self.assertRaises(ValueError):
            validate_test(state["proposal"], flow.directory / "source")
        state["next_node"] = "generate"
        self.assertEqual(flow.context(state)["previous_test"], state["proposal"])

    def test_changing_hits_and_rewording_do_not_hide_unresolved_setup(self):
        flow = self.workflow()
        state = flow.prepare("agentic")
        answer = {"sufficient": False, "missing_categories": ["setup"], "missing": ["fixture"],
                  "queries": ["fixture"], "kinds": ["tests"], "path_glob": "*"}
        state["search_history"] = [search_signature(answer)]
        state["sources"] = [{"id": "first"}]
        state = flow.assess(state, answer)
        self.assertEqual(state["next_node"], "search")
        state["sources"] = [{"id": "completely-new"}]
        state = flow.assess(state, {**answer, "missing": ["initialization and helper"]})
        self.assertEqual(state["stop_reason"], "unresolved_evidence")
        self.assertEqual(state["test_runs"], 0)

    def test_untried_search_survives_same_gap_but_cannot_exceed_budget(self):
        flow = self.workflow(Config(retrieval="lexical", max_search_rounds=3))
        state = flow.prepare("agentic")
        answer = {"sufficient": False, "missing_categories": ["setup"], "missing": ["fixture"],
                  "queries": ["first"], "kinds": ["tests"], "path_glob": "*"}
        state["route"] = answer
        with patch.object(flow.retriever, "search", return_value=([{"id": "same", "text": "same"}], {})):
            for query in ("second", "third", "fourth"):
                state = flow.search(state, None)
                state = flow.assess(state, {**answer, "queries": [query]})
                self.assertEqual(state["next_node"], "search" if query != "fourth" else "freeze")
        self.assertEqual(state["stop_reason"], "search_budget")
        self.assertEqual(search_signature({**answer, "queries": [" FIRST  "]}), search_signature(answer))

    def test_context_expansion_reserves_space_for_previous_api_evidence(self):
        flow = self.workflow(Config(retrieval="lexical", top_k=2))
        state = flow.prepare("agentic")
        anchor = next(iter(flow.retriever.chunks.values()))
        earlier = [{"id": "api", "text": "essential API"}, {"id": "test", "text": "caller example"}]
        state.update(sources=[anchor] + earlier, route={"read_source_ids": [anchor["id"]]})
        state = flow.search(state, None)
        self.assertTrue({"api", "test"} <= {c["id"] for c in state["sources"]})
        self.assertLessEqual(len(state["sources"]), 4)

    def test_followup_search_keeps_one_test_setup_bundle_within_source_cap(self):
        flow = self.workflow(Config(retrieval="lexical", top_k=4))
        source = flow.directory / "source/pkg"
        source.mkdir()
        (source / "test_base.py").write_text("class Base:\n    ready = True\n")
        (source / "test_api.py").write_text(
            "from .test_base import Base\n"
            "class ApiTests(Base):\n"
            "    def test_api(self):\n        assert Base.ready\n"
            "    def test_another(self):\n        assert Base.ready\n")
        index = build_index(flow.directory, flow.config)
        flow.index = index
        flow.retriever = Retriever(index, flow.task, flow.config)
        anchor = next(c for c in index["chunks"] if c["symbol"] == "ApiTests.test_api")
        other_anchor = next(c for c in index["chunks"] if c["symbol"] == "ApiTests.test_another")
        bundle = [anchor] + [flow.retriever.chunks[key] for key in flow.retriever.context_ids(anchor["id"])]
        self.assertEqual({c["symbol"] for c in bundle} & {"ApiTests.test_api", "ApiTests", "<module>", "Base"},
                         {"ApiTests.test_api", "ApiTests", "<module>", "Base"})
        state = flow.prepare("agentic")
        state["route"] = {"queries": ["test api"], "kinds": ["tests"], "path_glob": "*"}
        first_hits = [anchor, other_anchor] + [c for c in bundle[1:] if c["id"] != other_anchor["id"]] + list(index["chunks"])
        with patch.object(flow.retriever, "search", return_value=(first_hits, {
                "test_pattern_ids": [anchor["id"], other_anchor["id"]], "context_ids": [c["id"] for c in bundle[1:]]})):
            state = flow.search(state, None)
        followup = [{**c, "id": "followup-%d" % i, "text": "new issue evidence"}
                    for i, c in enumerate(first_hits[:4])]
        with patch.object(flow.retriever, "search", return_value=(followup, {"context_ids": []})):
            state = flow.search(state, None)
        self.assertEqual(state["bundle_anchor_id"], anchor["id"])
        delivered = [c["id"] for c in state["sources"]]
        self.assertTrue({c["id"] for c in bundle} <= set(delivered))
        self.assertEqual(len(delivered), len(set(delivered)))
        self.assertLessEqual(len(delivered), 2 * flow.config.top_k)
        state["sources"] = ([other_anchor] + state["sources"])[:2 * flow.config.top_k]
        state["route"] = {"read_source_ids": [other_anchor["id"]]}
        state = flow.search(state, None)
        self.assertEqual(state["bundle_anchor_id"], other_anchor["id"])

    def test_full_keyword_hits_cannot_evict_prior_issue_evidence(self):
        flow = self.workflow(Config(retrieval="lexical", top_k=2))
        state = flow.prepare("agentic")
        evidence = list(flow.retriever.chunks.values())[:2]
        state.update(sources=evidence, route={"queries": ["setup"], "kinds": ["tests"], "path_glob": "*"})
        evidence_ids = {source['id'] for source in evidence}
        for round_number in range(3):
            hits = [{'id': '%d-%d' % (round_number, n), 'text': 'setup'} for n in range(4)]
            trace = {'context_ids': [hit['id'] for hit in hits[2:]], 'context_truncated': False}
            with patch.object(flow.retriever, 'search', return_value=(hits, trace)):
                state = flow.search(state, None)
            delivered = {source['id'] for source in state['sources']}
            self.assertTrue(evidence_ids <= delivered)
            self.assertTrue({hit['id'] for hit in hits[:2]} <= delivered)
            self.assertEqual(len(delivered), len(state['sources']))
            self.assertLessEqual(len(delivered), 4)
            self.assertTrue(state['search_trace']['context_truncated'])
            self.assertEqual(state['search_trace']['context_ids'], [])

    def test_keyword_retention_deduplicates_and_fills_spare_context_slots(self):
        flow = self.workflow(Config(retrieval="lexical", top_k=2))
        state = flow.prepare("agentic")
        hits = [{'id': str(n), 'text': 'setup'} for n in range(4)]
        state.update(sources=[hits[0]], route={"queries": ["setup"], "kinds": ["tests"], "path_glob": "*"})
        with patch.object(flow.retriever, 'search', return_value=(hits, {'context_ids': ['2', '3']})):
            state = flow.search(state, None)
        self.assertEqual([source['id'] for source in state['sources']], ['0', '1', '2', '3'])
        self.assertFalse(state['search_trace']['context_truncated'])

    def test_resolved_gap_allows_progress_and_sufficient_evidence_generates(self):
        flow = self.workflow()
        state = flow.prepare("agentic")
        answer = {"sufficient": False, "missing_categories": ["api", "setup"], "missing": ["both"],
                  "queries": ["fixture"], "kinds": ["tests"], "path_glob": "*"}
        state["sources"] = [{"id": "first"}]
        state = flow.assess(state, answer)
        state["sources"] = [{"id": "second"}]
        state = flow.assess(state, {**answer, "missing_categories": ["setup"]})
        self.assertEqual(state["next_node"], "search")
        self.assertEqual(state["unresolved_rounds"], 1)
        state = flow.assess(state, {**answer, "sufficient": True, "missing_categories": []})
        self.assertEqual(state["next_node"], "generate")
        self.assertEqual(state["unresolved_rounds"], 0)

    def test_direct_context_read_counts_as_search_without_keyword_search(self):
        flow = self.workflow()
        state = flow.prepare("agentic")
        anchor = next(iter(flow.retriever.chunks.values()))
        state.update(sources=[anchor], route={"read_source_ids": [anchor["id"]]})
        with patch.object(flow.retriever, "search", side_effect=AssertionError("No keyword search")):
            state = flow.search(state, None)
        self.assertEqual(state["search_rounds"], 1)
        self.assertEqual(state["search_trace"]["mode"], "source_context")
        self.assertGreater(flow.context(state)["source_summary"]["available_count"], 0)
        self.assertEqual(flow.context(state)["source_summary"]["retained_from_previous_search"], 0)

    def test_followup_search_retains_bounded_evidence_and_rephrasing_cannot_fake_progress(self):
        flow = self.workflow(Config(retrieval="lexical", top_k=2))
        state = flow.prepare("agentic")
        state["route"] = {"queries": ["normalize"], "kinds": ["code"], "path_glob": "*"}
        def hit(n): return {"id": str(n), "text": str(n)}
        with patch.object(flow.retriever, "search", side_effect=[([hit(1), hit(2)], {}), ([hit(3), hit(4)], {}), ([hit(5), hit(4)], {})]):
            state = flow.search(state, None)
            state = flow.search(state, None)
            self.assertEqual([h["id"] for h in state["sources"]], ["3", "4", "1", "2"])
            state = flow.search(state, None)
        self.assertEqual([h["id"] for h in state["sources"]], ["5", "4", "3", "1"])
        self.assertEqual(state["search_trace"]["retained_ids"], ["3", "1"])
        answer = {**state["route"], "sufficient": False, "missing": ["method"], "contradictions": [], "reason": "missing"}
        for missing in ["method", "method body", "implementation"]:
            state["sources"].reverse()
            state = flow.assess(state, {**answer, "missing": [missing]})
        self.assertEqual(state["stop_reason"], "no_progress")

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
                answer.update(sufficient=False, missing=["fixture"], missing_categories=["setup"])
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

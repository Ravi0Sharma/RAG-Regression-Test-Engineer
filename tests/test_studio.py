"""Offline checks for the optional Studio graphs; no credentials or cloud calls."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec("langgraph"), "Install .[studio] for graph integration")
class StudioTests(unittest.TestCase):
    def setUp(self):
        from rag_regression import studio
        self.studio = studio
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        root_patch = patch.object(studio, "RUNS_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        # Never upload test fixtures even if the developer's shell enables tracing.
        from langsmith import tracing_context
        self.enterContext(tracing_context(enabled=False))

    def test_visible_loop_then_separate_frozen_evaluation(self):
        updates = list(self.studio.graph.stream({"mode": "agentic"}, stream_mode="updates"))
        names = [name for update in updates for name in update]
        self.assertEqual(names, ["prepare", "plan", "search", "assess", "generate", "execute", "decide",
                                 "search", "assess", "generate", "execute", "decide", "freeze"])
        executions = [update["execute"]["result"] for update in updates if "execute" in update]
        self.assertEqual([r["status"] for r in executions], ["syntax_import_error", "test_failure"])
        serialized = json.dumps(updates)
        self.assertNotIn("demo-only-strip-fix", serialized)
        self.assertNotIn("return value.strip().lower()", serialized)
        self.assertNotIn("api_key", serialized.lower())
        run_id = updates[0]["prepare"]["run_id"]
        frozen = updates[-1]["freeze"]["frozen"]
        result = self.studio.evaluation_graph.invoke({"run_id": run_id})["evaluation"]
        self.assertTrue(result["fail_to_pass"])
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(result["sha256"], frozen["sha256"])
        self.assertFalse((self.root / run_id / "generation/evaluation/review.json").exists())
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.studio.evaluation_graph.invoke({"run_id": run_id})

    def test_baseline_and_repeated_runs_are_isolated(self):
        first = self.studio.graph.invoke({"mode": "baseline"})
        second = self.studio.graph.invoke({"mode": "baseline"})
        self.assertNotEqual(first["run_id"], second["run_id"])
        for state in (first, second):
            self.assertEqual(state["test_versions"], 1)
            self.assertEqual(state["result"]["status"], "syntax_import_error")
            self.assertFalse(state["candidate"])
            self.assertEqual(state["summary"]["model"], "scripted-demo")
            self.assertEqual(state["summary"]["api_calls"], 0)

    def test_bad_mode_and_artifact_path_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "mode"):
            self.studio.graph.invoke({"mode": "unknown"})
        with self.assertRaisesRegex(ValueError, "run_id"):
            self.studio.evaluation_graph.invoke({"run_id": "../../outside"})

    def test_studio_edit_cannot_execute_arbitrary_python(self):
        state = self.studio.prepare({"mode": "agentic"})
        sentinel = self.root / "must-not-exist"
        state.update(test_versions=1, proposal={"path": "test_rag_regression.py",
            "code": "from pathlib import Path\nPath(" + repr(str(sentinel)) + ").touch()",
            "expected_failure": "not a bundled fixture"})
        result = self.studio.execute(state)
        self.assertEqual(result["result"]["status"], "invalid_test")
        self.assertFalse(sentinel.exists())


if __name__ == "__main__":
    unittest.main()

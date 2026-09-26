"""Deterministic synthetic demonstration, never a SWE-bench/model score."""

import subprocess
import sys
import tempfile
from pathlib import Path

from .agent import run_agent
from .core import Budget, Config, Events, read_json, write_json
from .evaluation import evaluate, report, review
from .retrieval import build_index
from .runner import classify

BUG = 'def normalize(value):\n    return value.lower()\n'
FIXED = 'def normalize(value):\n    return value.strip().lower()\n'
CORRECT = '''import unittest
from labels import normalize

class Regression(unittest.TestCase):
    def test_surrounding_spaces(self):
        self.assertEqual(normalize(" Ada "), "ada")
'''
WRONG = CORRECT.replace("from labels import normalize", "from labels import normalize_label as normalize")
PATCH = "demo-only-strip-fix"


class ScriptedModel:
    def __init__(self, events):
        self.generations, self.events = 0, events

    def ask(self, stage, context):
        route = {"queries": ["normalize whitespace", "normalize test imports", "labels normalize"],
                 "kinds": ["code", "tests"], "path_glob": "*", "reason": "Find implementation and existing imports"}
        if stage == "plan":
            answer = {**route, "needs": ["normalization behavior", "test imports"]}
        elif stage == "assess":
            answer = {**route, "sufficient": True, "missing": [], "contradictions": []}
        elif stage == "generate":
            self.generations += 1
            answer = {"path": "test_rag_regression.py", "code": WRONG if self.generations == 1 else CORRECT,
                      "expected_failure": "Surrounding spaces remain in result", "reason": "Exercise issue input"}
        else:
            failed = context["result"]["status"] == "test_failure"
            answer = {**route, "action": "candidate" if failed else "search", "relevant": failed,
                      "reason": "Whitespace assertion reproduced" if failed else "Find correct import after import failure"}
        self.events.emit(stage, result=answer, synthetic=True)
        return answer


class DemoRunner:
    """Only executes the two hardcoded demo tests, never arbitrary model code."""
    def __init__(self, task, config, runtime=None):
        self.runtime = {"backend": "unittest", "image": "synthetic-host-demo"}

    def run(self, test, directory, timeout, patch=None):
        if test["code"] not in {WRONG, CORRECT} or patch not in {None, PATCH}:
            raise ValueError("Demo runner accepts only bundled synthetic fixtures")
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=False)
        with tempfile.TemporaryDirectory() as temporary:
            working = Path(temporary)
            (working / "labels.py").write_text(FIXED if patch else BUG, encoding="utf-8")
            (working / "test_rag_regression.py").write_text(test["code"], encoding="utf-8")
            execution = (directory / "execution.json").resolve()
            process = subprocess.run([sys.executable, str(Path(__file__).with_name("runtime.py")),
                "unittest", "test_rag_regression.py", str(execution)], cwd=working,
                text=True, capture_output=True, timeout=timeout)
            log = process.stdout + process.stderr
            (directory / "output.log").write_text(log, encoding="utf-8")
            result = classify(read_json(execution), process.returncode, log)
            write_json(directory / "result.json", result)
            return result


def prepare_demo(destination):
    """Create the shared synthetic fixture without running or evaluating an agent."""
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Demo output exists; select a new directory")
    task_directory = destination / "tasks/public/demo__labels-1"
    source = task_directory / "source"
    source.mkdir(parents=True)
    (source / "labels.py").write_text(BUG, encoding="utf-8")
    (source / "test_labels.py").write_text('from labels import normalize\n\ndef test_lower():\n    assert normalize("ADA") == "ada"\n', encoding="utf-8")
    task = {"instance_id": "demo__labels-1", "repo": "demo/labels", "base_commit": "a" * 40,
            "version": "1", "problem_statement": "normalize should strip surrounding whitespace before lowercasing."}
    write_json(task_directory / "task.json", task)
    oracle = destination / "tasks/oracle/demo__labels-1.json"
    write_json(oracle, {"instance_id": task["instance_id"], "base_commit": task["base_commit"], "patch": PATCH})
    selection = destination / "tasks/selection.json"
    write_json(selection, {"ids": [task["instance_id"]], "dataset": "synthetic-demo"})
    config = Config(retrieval="lexical")
    build_index(task_directory, config)
    return task_directory, oracle, selection


def demo(destination):
    destination = Path(destination)
    task_directory, oracle, selection = prepare_demo(destination)
    task = read_json(task_directory / "task.json")
    config = Config(retrieval="lexical")
    for mode in ("baseline", "agentic"):
        output = destination / "runs" / mode / task["instance_id"]
        output.mkdir(parents=True)
        events = Events(output / "events.jsonl")
        run_agent(task_directory, output, mode, config, ScriptedModel(events), Budget(config),
                  events, DemoRunner(task, config))
        result = evaluate(output, oracle, runner_factory=DemoRunner)
        if result["fail_to_pass"]:
            review(output, "relevant", "Synthetic fixture: assertion directly checks documented whitespace bug; inspected fixed implementation.")
    totals = report(destination / "runs", selection, destination / "report")
    (destination / "report/DEMO.txt").write_text("Synthetic scripted demo only. Not SWE-bench results or evidence of model quality.\n")
    return totals

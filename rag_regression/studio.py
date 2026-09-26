"""Inspectable, scripted demo graphs. Never call a model API or execute arbitrary code."""

from dataclasses import asdict
import json
from pathlib import Path
import re
import time
from typing import Literal
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from rag_regression.core import Config, Events, digest, read_json, write_json
from rag_regression.demo import DemoRunner, ScriptedModel, prepare_demo
from rag_regression.evaluation import evaluate
from rag_regression.model import SCHEMAS, validate_schema
from rag_regression.retrieval import Retriever
from rag_regression.runner import validate_test


RUNS_ROOT = Path(__file__).resolve().parent.parent / "runs" / "studio"
CONFIG = Config(model="scripted-demo", retrieval="lexical")


class DemoInput(TypedDict, total=False):
    mode: Literal["agentic", "baseline"]


class DemoState(DemoInput, total=False):
    run_id: str
    task: dict
    started: float
    route: dict
    sources: list[dict]
    search_trace: dict
    assessment: dict
    proposal: dict
    test: dict
    result: dict
    decision: dict
    model_input: dict
    search_rounds: int
    test_versions: int
    test_runs: int
    candidate: bool
    stop_reason: str
    next_node: str
    explanation: str
    frozen: dict
    summary: dict


def run_paths(run_id):
    """Only our generated IDs can select local demo artifacts."""
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("Expected the run_id from regression_demo")
    root = RUNS_ROOT / run_id
    return root, root / "tasks/public/demo__labels-1", root / "generation"


def update(state, node, explanation, **changes):
    _, _, output = run_paths(state["run_id"])
    changes["explanation"] = explanation
    Events(output / "events.jsonl").emit("node", node=node, updates=changes)
    return changes


def ask(state, stage, extra):
    _, _, output = run_paths(state["run_id"])
    context = {"task": state["task"], "test_backend": "unittest", **extra}
    model = ScriptedModel(Events(output / "events.jsonl"))
    # Reconstruct from graph state, rather than sharing a mutable model across runs.
    model.generations = state["test_versions"]
    answer = model.ask(stage, context)
    validate_schema(answer, SCHEMAS[stage])
    return context, answer


def prepare(state: DemoInput):
    mode = state.get("mode", "agentic")
    if mode not in {"baseline", "agentic"}:
        raise ValueError("mode must be baseline or agentic")
    run_id = uuid4().hex
    root, task_directory, output = run_paths(run_id)
    prepare_demo(root)
    task = read_json(task_directory / "task.json")
    write_json(output / "task.json", task)
    write_json(output / "config.json", asdict(CONFIG))
    initial = {
        "mode": mode, "run_id": run_id, "task": task, "started": time.time(),
        "route": {}, "sources": [], "search_trace": {}, "assessment": {},
        "proposal": {}, "test": {}, "result": {}, "decision": {}, "model_input": {},
        "search_rounds": 0, "test_versions": 0, "test_runs": 0,
        "candidate": False, "stop_reason": "", "next_node": "plan",
        "frozen": {}, "summary": {},
    }
    return update(initial, "prepare", "Laddat leksaksbuggen och byggt ett lokalt ordindex. Inga modell-API-anrop.", **initial)


def plan(state: DemoState):
    context, route = ask(state, "plan", {})
    return update(state, "plan", "Planen väljer sökfrågor, materialtyper och filfilter.",
                  model_input=context, route=route, next_node="search")


def search(state: DemoState):
    limit = 1 if state["mode"] == "baseline" else CONFIG.max_search_rounds
    if state["search_rounds"] >= limit:
        return update(state, "search", "Sökbudgeten är slut.", stop_reason="search_budget", next_node="freeze")
    _, task_directory, _ = run_paths(state["run_id"])
    retriever = Retriever(read_json(task_directory / "index.json"), state["task"], CONFIG)
    route = state["route"]
    sources, trace = retriever.search(route["queries"], route["kinds"], route["path_glob"])
    return update(state, "search", "Ordsökning och RRF hittade kodbitar. Öppna sources för text, fil och radnummer.",
                  sources=sources, search_trace=trace, search_rounds=state["search_rounds"] + 1, next_node="assess")


def assess(state: DemoState):
    context, assessment = ask(state, "assess", {"sources": state["sources"], "previous_result": state["result"] or None})
    repeat = not assessment["sufficient"] and state["mode"] == "agentic"
    return update(state, "assess", "Kontrollerar om underlaget räcker. Demots fasta svar säger ja.",
                  model_input=context, assessment=assessment,
                  route=assessment if repeat else state["route"], next_node="search" if repeat else "generate")


def generate(state: DemoState):
    limit = 1 if state["mode"] == "baseline" else CONFIG.max_test_versions
    if state["test_versions"] >= limit:
        return update(state, "generate", "Testversionsbudgeten är slut.", stop_reason="test_version_budget", next_node="freeze")
    context, proposal = ask(state, "generate", {"sources": state["sources"], "previous_test": state["test"] or None,
                                                "previous_result": state["result"] or None})
    return update(state, "generate", "Första fasta testförslaget har fel import. Nästa förslag rättar importen.",
                  model_input=context, proposal=proposal, test_versions=state["test_versions"] + 1, next_node="execute")


def execute(state: DemoState):
    _, task_directory, output = run_paths(state["run_id"])
    proposal = state["proposal"]
    test, test_runs = state["test"], state["test_runs"]
    try:
        validate_test(proposal, task_directory / "source")
        test = proposal
        test_runs += 1
        # DemoRunner only permits the two bundled test strings, including after Studio edits.
        result = DemoRunner(state["task"], CONFIG).run(
            test, output / ("attempt-" + str(state["test_versions"])), CONFIG.test_timeout)
    except ValueError as invalid:
        result = {"status": "invalid_test", "detail": str(invalid), "executed": 0}
    return update(state, "execute", "Kört det fasta testet i en separat Python-process. Importfel visar inte buggen.",
                  test=test, result=result, test_runs=test_runs, next_node="decide")


def decide(state: DemoState):
    context, decision = ask(state, "decide", {"test": state["proposal"], "result": state["result"], "sources": state["sources"]})
    candidate = decision["action"] == "candidate" and decision["relevant"] and state["result"]["status"] == "test_failure"
    reason = ""
    if candidate:
        reason = "candidate"
    elif state["mode"] == "baseline":
        reason = "single_pass_complete"
    elif decision["action"] == "stop":
        reason = "agent_stop"
    elif state["test_versions"] >= CONFIG.max_test_versions:
        reason = "test_version_budget"
    next_node = "freeze" if reason else "search" if decision["action"] == "search" else "generate"
    return update(state, "decide", "Kandidat kräver ett utfört testfel. Annars väljs sökning, ändring eller stopp.",
                  model_input=context, decision=decision, route=decision, candidate=candidate,
                  stop_reason=reason, next_node=next_node)


def freeze(state: DemoState):
    _, task_directory, output = run_paths(state["run_id"])
    test = state["test"]
    frozen = {}
    if test:
        frozen = {"path": test["path"], "code": test["code"], "sha256": digest(test["code"]),
                  "expected_failure": test["expected_failure"], "instance_id": state["task"]["instance_id"],
                  "base_commit": state["task"]["base_commit"],
                  "source_sha256": read_json(task_directory / "index.json")["source_sha256"],
                  "runtime": DemoRunner(state["task"], CONFIG).runtime}
        frozen["artifact_sha256"] = digest(json.dumps(frozen, sort_keys=True))
        write_json(output / "frozen.json", frozen)
    summary = {"instance_id": state["task"]["instance_id"], "base_commit": state["task"]["base_commit"],
               "mode": state["mode"], "model": "scripted-demo", "synthetic": True,
               "candidate": state["candidate"], "stop_reason": state["stop_reason"],
               "search_rounds": state["search_rounds"], "test_versions": state["test_versions"],
               "test_runs": state["test_runs"], "last_result": state["result"],
               "frozen_sha256": frozen.get("sha256"), "api_calls": 0,
               "input_tokens": 0, "output_tokens": 0, "embedding_tokens": 0,
               "estimated_cost_usd": 0, "seconds": round(time.time() - state["started"], 3)}
    write_json(output / "summary.json", summary)
    return update(state, "freeze", "Testet är fryst. Kopiera run_id till den separata grafen evaluate_demo.",
                  frozen=frozen, summary=summary, next_node="end")


def next_node(state: DemoState):
    return state["next_node"]


def build_demo_graph():
    builder = StateGraph(DemoState, input_schema=DemoInput)
    for function in (prepare, plan, search, assess, generate, execute, decide, freeze):
        builder.add_node(function.__name__, function)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "plan")
    builder.add_edge("plan", "search")
    builder.add_conditional_edges("search", next_node, ["assess", "freeze"])
    builder.add_conditional_edges("assess", next_node, ["search", "generate"])
    builder.add_conditional_edges("generate", next_node, ["execute", "freeze"])
    builder.add_edge("execute", "decide")
    builder.add_conditional_edges("decide", next_node, ["search", "generate", "freeze"])
    builder.add_edge("freeze", END)
    return builder.compile(name="Regression demo — fasta svar")


class EvaluationInput(TypedDict):
    run_id: str


class EvaluationState(EvaluationInput, total=False):
    evaluation: dict
    explanation: str


def evaluate_demo(state: EvaluationInput):
    root, _, output = run_paths(state["run_id"])
    result = evaluate(output, root / "tasks/oracle/demo__labels-1.json", runner_factory=DemoRunner)
    return {"evaluation": result,
            "explanation": "Samma test kördes före och efter fixen. pending_review betyder att relevansgranskning återstår."}


graph = build_demo_graph()
evaluation_builder = StateGraph(EvaluationState, input_schema=EvaluationInput)
evaluation_builder.add_node("evaluate_frozen_test", evaluate_demo)
evaluation_builder.add_edge(START, "evaluate_frozen_test")
evaluation_builder.add_edge("evaluate_frozen_test", END)
evaluation_graph = evaluation_builder.compile(name="Utvärdera fryst demo — separat facit")

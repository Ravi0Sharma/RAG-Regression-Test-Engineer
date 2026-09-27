"""Studio adapter for the shared SWE-bench workflow and manual model responses."""

from typing import Literal
from typing_extensions import TypedDict
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from rag_regression.evaluation import evaluate
from rag_regression.handoff import Handoff
from rag_regression.model import SCHEMAS
from rag_regression.workflow import STAGES


class HandoffInput(TypedDict, total=False):
    instance_id: str
    mode: Literal["agentic", "baseline"]
    run_id: str


class HandoffState(HandoffInput, total=False):
    task: dict
    route: dict
    sources: list[dict]
    search_trace: dict
    assessment: dict
    proposal: dict
    test: dict | None
    result: dict | None
    decision: dict
    model_input: dict
    candidate: bool
    stop_reason: str
    error: str | None
    search_rounds: int
    test_versions: int
    test_runs: int
    stagnant: int
    unresolved_rounds: int
    setup_probe: bool
    search_history: list
    bundle_anchor_id: str | None
    previous_signature: str | None
    next_node: str
    frozen: dict | None
    summary: dict
    explanation: str
    revision: int
    request: dict | None
    response_imported: bool
    model_requests: int
    active_seconds: float


def build_handoff_graph(store=None, checkpointer=None):
    store = store or Handoff()

    def prepare(state: HandoffInput):
        if state.get("run_id"):
            return store.status(state["run_id"])
        return store.start(state["instance_id"], state.get("mode", "agentic"))

    def node(stage):
        def run(state: HandoffState):
            # Only ID/revision cross this boundary: edited Studio state is never executed.
            view = store.step(state["run_id"], state["revision"], pause=stage in SCHEMAS, expected_stage=stage)
            if view["request"] is not None:
                error = None
                while True:
                    response = interrupt({**view["request"], "validation_error": error,
                        "resume": "Paste the response envelope, or {request_id: ...} after CLI import."})
                    try:
                        if isinstance(response, dict) and set(response) == {"request_id"}:
                            current = store.status(state["run_id"])
                            if current["next_node"] != stage:
                                return current
                            if (response["request_id"] != current["request"]["request_id"]
                                or not current["response_imported"]):
                                raise ValueError("Import a matching response first")
                        else:
                            updated = store.submit(state["run_id"], response)
                            if updated["next_node"] != stage:
                                return updated
                            if updated["request"]["request_id"] != view["request"]["request_id"]:
                                view = updated
                                error = view["request"]["context"].get("validation_error")
                                continue
                        break
                    except ValueError as invalid:
                        error = str(invalid)
                        view = store.status(state["run_id"])
                        if view["next_node"] != stage:
                            return view
                view = store.step(state["run_id"], state["revision"], expected_stage=stage)
            return view
        run.__name__ = stage
        return run

    builder = StateGraph(HandoffState, input_schema=HandoffInput)
    builder.add_node("prepare", prepare)
    for stage in STAGES:
        builder.add_node(stage, node(stage))
    builder.add_edge(START, "prepare")
    routes = {stage: stage for stage in STAGES}
    routes["end"] = END
    transitions = {"prepare": list(routes), "plan": ["search", "freeze"],
                   "search": ["assess", "freeze"], "assess": ["search", "generate", "freeze"],
                   "generate": ["execute", "freeze"], "execute": ["decide", "freeze"],
                   "decide": ["search", "generate", "freeze"], "freeze": ["end"]}
    for stage, targets in transitions.items():
        builder.add_conditional_edges(stage, lambda state: state["next_node"], {target: routes[target] for target in targets})
    return builder.compile(checkpointer=checkpointer, name="SWE-bench dev — manuell modellöverlämning")


class EvaluationInput(TypedDict):
    run_id: str


class EvaluationState(EvaluationInput, total=False):
    evaluation: dict
    explanation: str


def evaluate_handoff(state: EvaluationInput):
    store = Handoff()
    view = store.status(state["run_id"])
    if view["next_node"] != "end" or not view["frozen"]:
        raise ValueError("Finish generation and freeze a test before evaluation")
    result = evaluate(store.directory(state["run_id"]) / "generation",
                      store.dev / "oracle" / (view["task"]["instance_id"] + ".json"))
    return {"evaluation": result, "explanation": "Separat före/efter-körning i Docker. Relevans granskas av människa."}


graph = build_handoff_graph()
evaluation_builder = StateGraph(EvaluationState, input_schema=EvaluationInput)
evaluation_builder.add_node("evaluate_frozen_test", evaluate_handoff)
evaluation_builder.add_edge(START, "evaluate_frozen_test")
evaluation_builder.add_edge("evaluate_frozen_test", END)
evaluation_graph = evaluation_builder.compile(name="SWE-bench dev — separat utvärdering")

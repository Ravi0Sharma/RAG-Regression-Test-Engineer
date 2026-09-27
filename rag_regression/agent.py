"""CLI driver for the shared bounded workflow."""

from .core import BudgetExceeded
from .model import SCHEMAS, InvalidSourceRequest, MAX_SOURCE_REPAIRS, source_repair_context
from .workflow import Workflow


def run_agent(task_directory, output, mode, config, model, budget, events, runner):
    workflow = Workflow(task_directory, output, config, budget, events, runner, getattr(model, 'embed', None))
    state = workflow.prepare(mode)
    while state['next_node'] != 'end':
        state = workflow.gate(state)
        stage = state['next_node']
        try:
            context = workflow.context(state)
            for attempt in range(MAX_SOURCE_REPAIRS + 1):
                answer = model.ask(stage, context) if stage in SCHEMAS else None
                try:
                    state = workflow.step(state, answer)
                    break
                except InvalidSourceRequest as invalid:
                    events.emit('rejected_source_request', stage=stage, answer=answer,
                                error=str(invalid), repair_attempt=attempt)
                    if attempt == MAX_SOURCE_REPAIRS:
                        state = workflow.stop(state, 'invalid_source_request', str(invalid))
                        break
                    budget.check(api=True)
                    context = source_repair_context(context, answer, invalid)
        except BudgetExceeded as exceeded:
            state = workflow.stop(state, str(exceeded))
        except (RuntimeError, ValueError, KeyError, OSError) as failure:
            if stage == 'freeze':
                raise
            events.emit('error', message=str(failure))
            state = workflow.stop(state, 'run_error', str(failure))
    return state['summary']

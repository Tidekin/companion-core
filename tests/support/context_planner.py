from types import SimpleNamespace

from pydantic_ai.messages import repair_messages
from pydantic_ai.models import ModelRequestParameters

from companion import context_planner as planner
from companion.agent_messages import from_wire_messages


def initial(core, job, *, references=1, turns=1, snapshots=None):
    snapshots = [] if snapshots is None else snapshots
    rules = [{"role": "system", "content": "trusted rules"}]
    refs = [
        [{"role": "user", "content": "optional-reference-" + str(i) + " R" * 700}]
        for i in range(references)
    ]
    history = [
        [
            {"role": "user", "content": "old question" + " H" * 200},
            {"role": "assistant", "content": "old answer" + " A" * 200},
        ]
        for _ in range(turns)
    ]
    current = [{"role": "user", "content": "complete current input"}]
    blocks = [
        ("rules", rules, True, 4),
        *(("reference", ref, False, 1) for ref in refs),
        *(("history", turn, False, 2) for turn in history),
        ("current", current, True, 4),
    ]
    messages = [message for _, block, _, _ in blocks for message in block]
    planner.initialize(core, job, messages, blocks, snapshots)
    return messages, snapshots


def request_context(messages, *, parameters=None):
    return SimpleNamespace(
        messages=repair_messages(from_wire_messages(messages), repair_last_response=False),
        model_request_parameters=parameters or ModelRequestParameters(),
    )

from companion.contracts import LeaseRequest
from companion.model import ModelResponse
from companion.transport import lease_deliveries
from companion.turn_taking import TurnTakingConfig
from tests.conftest import ingest, run


class Model:
    def __init__(self):
        self.calls, self.chat, self.activity = [], [], []

    async def complete(self, messages, scenario):
        self.calls.append((messages, scenario))
        queue = self.chat if scenario == "chat" else self.activity
        return ModelResponse(queue.pop(0) if queue else "嗯嗯，我也这么觉得。", 100, 20, 40)


def enable(core, **values):
    core.settings.turn_taking = TurnTakingConfig(**values)
    core.settings.proactive_enabled = True
    core.settings.quiet_start = core.settings.quiet_end = 0
    core.settings.context_soft_tokens = 12000
    core.model = Model()


def turn(core, number, text, answer=None, *, deliver=True):
    """One owner message, its chat job, and (optionally) the delivered reply."""
    if answer is not None:
        core.model.chat.append(answer)
    result = ingest(core, number, text)
    assert run(core.run_once("worker"))
    if deliver:
        core.store.execute(
            (
                "UPDATE outbox SET state='SENT',sent_at=%s WHERE "
                "origin_event_id=%s AND state='PENDING'"
            ), (core.clock(), result["event_id"]))
    return result


def system(core, index=-1):
    return core.model.calls[index][0][0]["content"]


def history_messages(messages):
    return [message for message in messages if message["role"] == "user"
            and isinstance(message["content"], str) and message["content"].startswith("【")]


def lease(core):
    return lease_deliveries(core.store, core.settings, LeaseRequest(worker_id="bridge"),
                            core.clock())["items"]


def lively(core):
    turn(core, 1, "我刚看完那部电影", "结局你喜欢吗？")
    core.clock.advance(60)
    return turn(core, 2, "还行，就是有点仓促", "我也觉得后半段太赶了。")

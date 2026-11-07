from companion.model import ModelResponse
from tests.support.source_definitions import accepted_text, enable_sharing


class NoticeModel:
    def __init__(self, text):
        self.text, self.calls = text, []

    async def complete(self, messages, scenario):
        self.calls.append((messages, scenario))
        assert scenario == "activity"
        return ModelResponse(self.text, 100, 20)


def ready_share(core, text):
    enable_sharing(core)
    event = accepted_text(core)
    core.observations.append("reader", event)
    core.social.consider(event.event_id)
    core.model = NoticeModel(text)
    assert core.social.flush()
    return event

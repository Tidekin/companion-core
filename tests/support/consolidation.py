import json

from companion.model import ModelResponse
from companion.store import encode
from tests.support.diary import DiaryModel, seed_day


class ReflectionModel(DiaryModel):
    def __init__(self):
        super().__init__()
        self.reflection_callback = None
        self.reflection_transform = None
        self.reflection_error = None

    async def complete(self, messages, scenario):
        if scenario != "reflection":
            return await super().complete(messages, scenario)
        self.calls.append((messages, scenario))
        if self.reflection_callback:
            self.reflection_callback()
        if self.reflection_error:
            raise self.reflection_error
        source = json.loads(messages[-1]["content"])["sources"][0]
        draft = {
            "suggestions": [
                {
                    "kind": "next_question",
                    "text": "下次可以再了解这个主题。",
                    "evidence": [{"source_id": source["source_id"], "exact_quote": source["text"]}],
                }
            ]
        }
        if self.reflection_transform:
            self.reflection_transform(draft)
        return ModelResponse(encode(draft), 100, 60, 10)


def enable_nightly(core, seed=True):
    if seed:
        seed_day(core)
    core.settings.consolidation_enabled = True
    core.settings.reflection_enabled = True
    model = ReflectionModel()
    core.model = core.diary.model = core.reflections.model = model
    return model

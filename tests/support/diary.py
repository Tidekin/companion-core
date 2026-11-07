import json
from datetime import date

from companion.model import ModelResponse
from companion.store import encode
from tests.conftest import FakeModel, ingest, run

DAY = date(2026, 10, 18)


class DiaryModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.transform = None

    async def complete(self, messages, scenario):
        if scenario != "diary":
            return await super().complete(messages, scenario)
        self.calls.append((messages, scenario))
        if self.callback:
            self.callback()
        if self.error:
            raise self.error
        data = json.loads(messages[-1]["content"])
        source = next(row for row in data["sources"] if row["kind"] == "owner_message")
        draft = {
            "schema_version": 1,
            "local_date": data["local_date"],
            "title": "关于今天的交流",
            "worth_saving": True,
            "sections": [
                {
                    "kind": "observation",
                    "text": source["text"],
                    "evidence": [{"source_id": source["source_id"], "exact_quote": source["text"]}],
                },
                {
                    "kind": "question",
                    "text": "下次可以再问问这件事。",
                    "evidence": [{"source_id": source["source_id"], "exact_quote": ""}],
                },
            ],
        }
        if self.transform:
            self.transform(draft)
        return ModelResponse(encode(draft), 140, 80, 20)


def seed_day(core, texts=None, day=DAY):
    core.settings.diary_enabled = True
    model = DiaryModel()
    core.diary.model = model
    texts = texts or [
        "今天讨论了我的读书计划。",
        "这周想先读科幻小说。",
        "我喜欢先列出章节再开始阅读。",
    ]
    for i, text in enumerate(texts, 1):
        ingest(core, i, text, at=day.isoformat() + "T04:00:00+00:00")
    # Historical owner events are real accepted inbox records; their chat jobs already ended.
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    return model


def generate(core):
    core.diary.schedule_day(DAY)
    assert run(core.diary.generate_once("diary-worker"))
    return core.store.one("SELECT * FROM diary_runs WHERE local_date=?", (DAY.isoformat(),))

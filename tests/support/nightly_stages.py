from companion.store import encode
from companion.worker_schedule import UnitResult
from tests.conftest import run
from tests.support.consolidation import enable_nightly
from tests.support.diary import DAY


def staged(core):
    model = enable_nightly(core)
    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 3
    core.settings.extraction_idle_seconds = 0
    model.extraction = encode({"memories": [
        {"subject": "user", "kind": "preference", "fact_key": "reading-plan",
         "content": "我喜欢先列出章节再开始阅读。", "evidence_event_id": "evt_test_3",
         "evidence_quote": "我喜欢先列出章节再开始阅读。"},
        {"subject": "project", "kind": "fact", "fact_key": "reading-week",
         "content": "这周想先读科幻小说。", "evidence_event_id": "evt_test_2",
         "evidence_quote": "这周想先读科幻小说。"},
    ]})
    core.consolidation.schedule_day(DAY)
    return model


def step(core):
    before = core.store.one("SELECT COUNT(*) AS n FROM model_usage")["n"]
    result = run(core.consolidation.run_step("nightly-test"))
    assert isinstance(result, UnitResult)
    assert core.store.one("SELECT COUNT(*) AS n FROM model_usage")["n"] - before <= 1
    return result

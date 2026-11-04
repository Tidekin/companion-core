"""Stop after a real native fee settlement, before its business transaction."""

import json

import pytest

from companion import budget, jargon, style_distill, watch_ledger
from tests.conftest import run
from tests.test_jargon import ready as jargon_ready
from tests.test_jargon import say
from tests.test_style_distill import comment, thread
from tests.test_style_distill import ready as style_ready
from tests.test_watch_ledger import JUDGEMENT, observe
from tests.test_watch_ledger import ready as watch_ready


@pytest.mark.parametrize(("module", "table"), [
    (watch_ledger, "watch_ledger"), (style_distill, "style_pairs"), (jargon, "jargon_work"),
])
def test_settled_result_crash_is_terminal_without_recharging(core, tmp_path, monkeypatch,
                                                            module, table):
    if module is watch_ledger:
        provider = watch_ready(core, tmp_path, json.dumps(JUDGEMENT))
        observe(core, 1)
    elif module is style_distill:
        provider = style_ready(core, tmp_path, '{"none": true}')
        thread(core, 1, [comment("1", "这是合成的测试评论"), comment("2", "这条回复也很具体", "1")])
    else:
        for i in range(1, 5):
            say(core, i, "今天路上看到一只猫")
        provider = jargon_ready(core, tmp_path, {"terms": []})
    complete = budget.complete

    class Interrupted(BaseException):
        pass

    async def cut_after_settlement(*args, **kwargs):
        await complete(*args, **kwargs)
        raise Interrupted

    monkeypatch.setattr(budget, "complete", cut_after_settlement)
    with pytest.raises(Interrupted):
        run(module.run_once(core.store, core.settings, core.model, core.clock))
    [usage] = core.store.rows("SELECT * FROM model_usage")
    assert usage["state"] == "SETTLED"
    assert core.store.one(f"SELECT state FROM {table}")["state"] == "RUNNING"
    core.clock.advance(module.LEASE_SECONDS + 1)
    for _ in range(3):
        run(module.run_once(core.store, core.settings, core.model, core.clock))
    assert core.store.one(f"SELECT state,error_code,model_request_id FROM {table}") == {
        "state": "FAILED", "error_code": "MODEL_RESULT_NOT_CHECKPOINTED",
        "model_request_id": usage["request_id"]}
    assert core.store.rows("SELECT * FROM model_usage") == [usage]
    assert len(provider.calls) == 1

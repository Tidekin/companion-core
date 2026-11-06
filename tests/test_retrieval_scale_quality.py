"""Fresh Chinese paraphrases over native old sources; real pinned local encoder."""

import pytest

from companion.store import after, encode
from tests.test_m1 import remember
from tests.test_retrieval_work_bounds import assert_bounded


@pytest.mark.parametrize("scale", [120, 1200])
def test_old_facts_synonyms_corrections_and_negative_scope_at_scale(core, scale, record_property):
    facts = [
        ("camera", "我的相机收在湖蓝色硬壳箱里。", "camera_case"),
        ("visit", "青栎展览的入场日期是十二月九日，座位为B17。", "exhibition_visit"),
        ("code", "苍梧测绘项目的样本代号是KX-73，负责人是温芷。", "survey_code"),
        ("delivery", "项目资料放在南门接待处。", "project_delivery"),
        ("expired", "过期的青栎展览凭证座位是C99。", "expired_visit"),
        ("forgotten", "旅行箱里放着银色口琴。", "travel_box"),
        ("denied", "公开设定中的摄影器材在黑色软包里。", "public_camera"),
    ]
    ids = {name: remember(core, i, text, fact_key=key, confirmed=True)
           for i, (name, text, key) in enumerate(facts, 1)}
    core.clock.advance(60)
    old_delivery = ids["delivery"]
    ids["delivery"] = core.memory.decide(old_delivery, "correct", core.clock(),
                                         "项目资料现在放在西门值班室，南门接待处已停用。")['memory_id']
    core.store.execute("UPDATE memories SET valid_to=? WHERE memory_id=?",
                       (after(core.clock(), -1), ids["expired"]))
    core.memory.decide(ids["forgotten"], "forget", core.clock())
    core.store.execute("UPDATE memories SET privacy_scope='public' WHERE memory_id=?",
                       (ids["denied"],))
    core.clock.advance(60)
    for number in range(10, scale + 10):
        remember(core, number, f"离线压测样本{number}：天文馆发行纪念邮票。", confirmed=True)
    forbidden = {old_delivery, ids["expired"], ids["forgotten"], ids["denied"]}
    metrics = []
    for state in ("cold", "warm"):
        if state == "warm":
            # Test setup constructs the same derived cache in bulk. Production uses
            # warm_once's independently tested eight-row units, with live source pins.
            rows = core.store.rows("SELECT * FROM memories WHERE confidence>0 AND "
                                   "privacy_scope='private' AND status IN "
                                   "('CONFIRMED','HYPOTHESIS')")
            vectors, missing = core.memory.index._prepare(rows)
            inferred = core.memory.index._infer(
                "离线索引准备", tuple(r['content'] for r, _ in missing))
            with core.store.transaction():
                core.memory.index._finish(vectors, missing, inferred)
        for name, query in (("camera", "camera_case"), ("code", "KX-73"),
                            ("visit", "青栎展览十二月九日的座位"),
                            ("delivery", "项目资料现在放在哪里"),
                            ("camera", "摄影器材收在哪里"), (None, "我订了哪家酒店？")):
            trace = {}
            result = core.memory_context.recall(query, core.clock(), trace=trace)
            found = {row["memory_id"] for row in result["rows"]}
            assert not found & forbidden
            if name is None:
                assert not found
            elif state == "warm" or query != "摄影器材收在哪里":
                assert ids[name] in found, (
                    state, query, [row['content'] for row in result['rows']])
            elif ids[name] not in found:
                assert result["state"] == "COVERAGE_LIMITED"
            assert_bounded(trace["work"])
            metrics.append({"cache": state, "query": query, "expected_hit": (
                ids[name] in found if name else not found), "work": trace["work"]})
    assert not core.model.calls and not core.store.rows("SELECT * FROM model_usage")
    record_property("scale_quality", encode({
        "scale": scale, "cases": metrics, "provider_calls": 0}))

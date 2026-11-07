from companion.contracts import Candidate
from companion.memory_work import checkpoint_state
from companion.store import encode
from tests.conftest import ingest, run
from tests.support.confirmed_memory import remember

OLD = "杉羽借物柜编号是RQ-83。"


CORRECTION = "我拍了柜牌核对，杉羽借物柜正确是RP-93，原先的RQ-83写错了，不是换了柜子。"


CONTRADICTION = "台布是天蓝的。"


def candidate(text, *, update=None, content=None, number=2, key="cabinet"):
    return Candidate(subject="project", kind="fact", fact_key=key, content=content or text,
                     evidence_event_id=f"evt_test_{number}", evidence_quote=text,
                     speaker="owner", update=update)


def extraction(core, values):
    core.model.extraction = encode({"memories": [c.model_dump(mode="json") for c in values]})


def setup(core, *, correction=True):
    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 1
    core.settings.extraction_idle_seconds = 0
    old = remember(core, 1, OLD if correction else "台布是砖红的。",
                   subject="project", fact_key="cabinet")
    core.store.execute("UPDATE events SET memory_processed=1")
    text = CORRECTION if correction else CONTRADICTION
    ingest(core, 2, text)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    item = candidate(text, update=None if correction else "replaces_previous",
                     content=None if correction else "台布天蓝取代砖红，现在有效的是天蓝。")
    extraction(core, [item])

    def review(data):
        row = data["candidates"][0]
        assert row["quote"] == text
        return encode({"relations": [{"supported": correction or row["content"] == text,
            "update_authorized": correction,
            "claims_replacement": correction or row["content"] != text,
            "same": None, "conflicts": ["m0"],
            "replaces": ["m0"] if correction else []}]})

    core.model.review = review
    return old


def failed_semantic_batch(core):
    setup(core)
    core.settings.max_attempts = 1
    assert run(core.extract_step()).progressed
    assert run(core.extract_step()).progressed  # A terminal checkpoint is completed work.
    record = checkpoint_state(core.store)
    assert record["batch"]["phase"] == "FAILED"
    return record

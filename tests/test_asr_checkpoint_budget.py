"""A checkpoint writer must not rebuild the same ASR receipt twice per fact."""
import pytest

from companion import memory_references as refs
from companion.contracts import CoreError
from tests.support.memory_references import prepared


def test_writer_pin_validates_each_asr_reference_once_and_rechecks_later_writes(core, monkeypatch):
    _, item = prepared(core, "transcript")
    receipt = refs.capture(core.memory, "evt_test_1", "transcript", "约好8月9日到枫汀取凭条。",
                           {"native_receipt": "x" * 90000}, core.clock())
    keys = [core.memory.add(item.model_copy(update={
        "subject": "project", "fact_key": f"receipt_part_{index}",
        "content": f"取货凭条转写中的第{index}条记录。", "evidence_quote": "8月9日",
        "evidence_reference_id": receipt["reference_id"]}), core.clock()) for index in range(6)]
    source = core.store.one("SELECT * FROM events WHERE event_id=%s", ("evt_test_1",))
    selected = [core.memory_context._fact(core.store.one(
        "SELECT * FROM memories WHERE memory_id=%s", (key,)), [source]) for key in keys]
    proof = core.memory_context.pin(core.clock(), rows=selected)
    reads = []
    native_read = refs.read

    def read(*args, **kwargs):
        reads.append(args[1])
        return native_read(*args, **kwargs)

    monkeypatch.setattr(refs, "read", read)
    with core.store.transaction():
        core.memory_context.revalidate(proof, core.clock())
        assert reads.count(receipt["reference_id"]) == len(keys)
        core.store.execute("DELETE FROM runtime_meta WHERE key=%s",
                           (refs.INPUT + receipt["reference_id"],))
        with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
            core.memory_context.revalidate(proof, core.clock())

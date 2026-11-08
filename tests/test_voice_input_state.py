"""Read-only source gating and hash-only recovery for the Bridge voice uploader."""

from datetime import timedelta

import pytest

from companion.contracts import CoreError
from tests.support.voice_flow import flow_fixture, upload


@pytest.mark.parametrize("closed", ["expired", "running", "consumed"])
def test_input_state_never_reopens_expired_or_consumed_upload(core, monkeypatch, closed):
    flow, _, _, event_id, _ = flow_fixture(core, monkeypatch, make_reply=False)
    initial = flow.input_receipt(event_id)
    assert initial["records"][0]["upload_allowed"] is True
    if closed == "expired":
        core.clock.now += timedelta(seconds=core.settings.voice.asr.wait_seconds + 1)
    else:
        core.store.execute('UPDATE jobs SET state=%s WHERE event_id=%s',
                           ("RUNNING" if closed == "running" else "SUCCEEDED", event_id))
    with core.store.read_snapshot():
        result = flow.input_receipt(event_id)
        assert result["state"] == "FALLBACK"
        assert result["records"][0]["state"] == "MISSING"
        assert result["records"][0]["upload_allowed"] is False


def test_input_state_rejects_different_source_upload_hash(core, monkeypatch):
    flow, _, _, event_id, key = flow_fixture(core, monkeypatch, make_reply=False)
    upload(core, flow, event_id, key)
    core.store.execute('UPDATE voice_jobs SET source_input_sha256=%s', ("0" * 64,))
    with pytest.raises(CoreError, match="VOICE_INPUT_AUTHORITY_CHANGED"):
        flow.input_receipt(event_id)


def test_input_state_rejects_cancelled_source_without_recovery_mutation(core, monkeypatch):
    flow, _, _, event_id, key = flow_fixture(core, monkeypatch, make_reply=False)
    upload(core, flow, event_id, key)
    core.store.execute("UPDATE jobs SET state='CANCELLED' WHERE event_id=%s", (event_id,))
    with core.store.read_snapshot():
        with pytest.raises(CoreError):
            flow.input_receipt(event_id)


def test_input_state_selects_asr_node_after_an_enabled_tts_only_node(core, monkeypatch):
    from companion.voice_config import VoiceNode

    nodes = {
        "a_tts": VoiceNode(token="t" * 32, manifest_sha256="a" * 64, capabilities=("tts",)),
        "z_asr": VoiceNode(token="r" * 32, manifest_sha256="b" * 64, capabilities=("asr",)),
    }
    flow, _, _, event_id, key = flow_fixture(core, monkeypatch, make_reply=False, nodes=nodes)
    with core.store.read_snapshot():
        assert flow.input_receipt(event_id)["records"][0]["upload_allowed"] is True
    upload(core, flow, event_id, key)
    job = core.store.one("SELECT node_id FROM voice_jobs WHERE operation='asr'")
    assert job["node_id"] == "z_asr"

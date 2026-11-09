"""Erasure keeps only frozen non-content authority needed to resolve an attempted send."""

import hashlib
import json

import pytest

from companion.contracts import CoreError
from companion.store import encode
from companion.voice_jobs import erase_voice_jobs, fence_conversation, restore_voice_jobs
from tests.support.voice_flow import finish_node, flow_fixture, prepare


@pytest.mark.parametrize("action", ["erase", "restore", "revision"])
def test_voice_fence_retires_content_and_keeps_only_random_prior_delivery_proof(
    core, monkeypatch, action
):
    flow, jobs, message_id, event_id, _ = flow_fixture(core, monkeypatch)
    prepare(core, flow, message_id)
    finish_node(jobs)
    prepare(core, flow, message_id)
    key = "voice_reply:" + message_id
    original = json.loads(
        core.store.one('SELECT value FROM runtime_meta WHERE key=%s', (key,))["value"]
    )
    with core.store.transaction():
        if action == "erase":
            erase_voice_jobs(core.store, [event_id])
        elif action == "restore":
            restore_voice_jobs(core.store)
        else:
            conversation = core.store.one(
                'SELECT conversation_id FROM events WHERE event_id=%s', (event_id,)
            )["conversation_id"]
            fence_conversation(core.store, conversation)
    retained = json.loads(
        core.store.one('SELECT value FROM runtime_meta WHERE key=%s', (key,))["value"]
    )
    assert set(retained) == {"state", "reason", "job_id", "retired"}
    assert retained["state"] == "FENCED"
    assert retained["job_id"] == original["job_id"]
    proof = retained["retired"]
    assert proof["kind"] == "capability"
    assert proof["verifier"] == hashlib.sha256(
        original["binding"]["retirement_token"].encode()).hexdigest()
    for field in ("reply_sha256", "result_sha256", "source_sha256", "audio_sha256"):
        assert original["binding"][field] not in json.dumps(retained)
    row = core.store.one('SELECT * FROM voice_jobs WHERE job_id=%s', (retained["job_id"],))
    assert row["input_blob"] is None and row["result_blob"] is None
    assert row["result_sha256"] is None
    with pytest.raises(CoreError, match="VOICE_FLOW_FENCED"):
        prepare(core, flow, message_id)


def test_erasure_does_not_keep_arbitrary_fields_from_corrupt_voice_binding(core, monkeypatch):
    _, _, message_id, event_id, _ = flow_fixture(core, monkeypatch)
    key = "voice_reply:" + message_id
    core.store.insert(
        "runtime_meta",
        key=key,
        value=encode(
            {
                "state": "AUDIO",
                "binding": {"text": "private content"},
                "binding_sha256": "0" * 64,
                "audio_base64": "private audio",
            }
        ),
    )
    with core.store.transaction():
        erase_voice_jobs(core.store, [event_id])
    assert json.loads(
        core.store.one('SELECT value FROM runtime_meta WHERE key=%s', (key,))["value"]
    ) == {
        "state": "FENCED",
        "reason": "VOICE_SOURCE_ERASED",
    }

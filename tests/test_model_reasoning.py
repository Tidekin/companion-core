"""Provider reasoning keeps native SDK custody, owner access and source erasure."""

import asyncio
import json

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from companion.backup import backup, restore
from companion.cognition_read import CognitionRead
from companion.contracts import CoreError
from companion.core import Core
from companion.model import ModelAdmission, ModelClient, model_payload
from companion.paid_budget import sha
from companion.privacy import advance_epoch
from companion.rem_protocol import RemConfig
from tests.support.humanlike_defaults import plain
from tests.support.model_reasoning import reasoning_chat


def test_rem_defaults_enable_bounded_private_dreams():
    config = RemConfig()
    assert config.enabled
    assert config.local_hour == 4
    assert plain().rem == config
    assert RemConfig(enabled=False).enabled is False
    assert RemConfig().enabled
    with pytest.raises(ValidationError):
        RemConfig(max_input_tokens=0)


@pytest.mark.parametrize("reasoning", [None, "", {"unexpected": "value"}, "合成推理"])
def test_adapter_preserves_only_provider_reasoning_strings(core, reasoning):
    core.settings.mode = "live"
    messages = [{"role": "user", "content": "synthetic input"}]

    async def scenario():
        def provider(request):
            return httpx.Response(200, json={"choices": [{"message": {
                "content": "synthetic reply", "reasoning_content": reasoning}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            result = await ModelClient(core.settings, client).complete(messages, "chat",
                admission=ModelAdmission(sha(model_payload(core.settings, messages, "chat"))))
        assert result.reasoning_content == (reasoning if reasoning == "合成推理" else None)
        assert result.text == "synthetic reply" and result.output_tokens == 20

    asyncio.run(scenario())


@pytest.mark.parametrize("tool", [False, True])
def test_reasoning_is_saved_per_request_without_entering_reply_or_prompt(core, tmp_path, tool):
    _, job, usage, requests = reasoning_chat(core, tmp_path, tool=tool)
    assert core.store.one('SELECT state FROM jobs WHERE job_id=%s',
                          (job["job_id"],))["state"] == "SUCCEEDED"
    assert len(requests) == (2 if tool else 1)
    reader = CognitionRead(core)
    for index, row in enumerate(usage, start=1):
        page = reader.page("model", record_id=row["request_id"])
        assert page["items"][0]["reasoning"]["text"] == "合成的私人推理" + str(index)
        assert page["items"][0]["reasoning"]["state"] == "RECORDED"
    stored = core.store.one('SELECT conversation_json FROM agent_conversations WHERE job_id=%s',
                            (job["job_id"],))["conversation_json"]
    assert "合成的私人推理" in stored
    for request in requests:
        assert "合成的私人推理" not in json.dumps(request, ensure_ascii=False)
    assert "合成的私人推理" not in json.dumps(core.store.rows("SELECT * FROM outbox"),
                                                        ensure_ascii=False)


def test_empty_reply_retains_received_reasoning_and_settled_usage_without_replay(core, tmp_path):
    _, job, usage, requests = reasoning_chat(core, tmp_path, empty=True)
    result = CognitionRead(core).page("model", record_id=usage[0]["request_id"])
    assert result["items"][0]["reasoning"]["text"] == "合成的私人推理1"
    assert usage[0]["state"] == "SETTLED" and usage[0]["billed_micro_usd"] > 0
    assert "合成的私人推理" not in json.dumps(core.store.rows("SELECT * FROM outbox"),
                                                        ensure_ascii=False)
    assert not asyncio.run(core.run_once(job_id=job["job_id"]))
    assert len(requests) == 1


def test_missing_provider_reasoning_and_legacy_snapshot_are_explicit(core, tmp_path):
    _, job, usage, _ = reasoning_chat(core, tmp_path, reasoning=None)
    reader = CognitionRead(core)
    assert reader.page("model", record_id=usage[0]["request_id"])["items"][0]["reasoning"] == {
        "state": "NOT_RETURNED"}
    core.store.execute('UPDATE agent_conversations SET conversation_json=NULL WHERE job_id=%s',
                       (job["job_id"],))
    assert reader.page("model", record_id=usage[0]["request_id"])["items"][0]["reasoning"] == {
        "state": "NOT_RECORDED"}


def test_reasoning_redacts_credentials_before_storage_and_readback(core, tmp_path):
    secret = "Bearer synthetic-private-reasoning-credential"
    _, job, usage, _ = reasoning_chat(core, tmp_path, reasoning=secret)
    saved = core.store.one('SELECT conversation_json FROM agent_conversations WHERE job_id=%s',
                           (job["job_id"],))["conversation_json"]
    assert secret not in saved and "[REDACTED]" in saved
    item = CognitionRead(core).page("model", record_id=usage[0]["request_id"])["items"][0]
    assert item["reasoning"]["redacted"] and secret not in json.dumps(item)


def test_reasoning_source_erasure_scrubs_snapshots_and_preserves_accounting(
    empty_database, core, tmp_path,
):
    capture, job, usage, _ = reasoning_chat(core, tmp_path)
    before = core.store.rows("SELECT * FROM model_usage")
    archive = tmp_path / "reasoning.enc"
    restored_state = tmp_path / "restored-state"
    key = Fernet.generate_key()
    backup(core.settings.database, archive, key)
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, [capture.event_id], core.clock())
    assert core.store.one('SELECT conversation_json FROM agent_conversations WHERE job_id=%s',
                          (job["job_id"],))["conversation_json"] is None
    assert core.store.rows("SELECT * FROM model_usage") == before
    assert CognitionRead(core).page("model", record_id=usage[0]["request_id"])["items"] == []
    restore(archive, empty_database, key, core.settings.erasure_ledger,
            state_directory=restored_state)
    with Core(core.settings.model_copy(update={
        "database": empty_database, "state_directory": str(restored_state),
    }), core.model,
              core.clock) as restored:
        assert restored.store.one((
            'SELECT conversation_json FROM agent_conversations WHERE '
            'job_id=%s'
        ), (job["job_id"],))["conversation_json"] is None
        assert restored.store.rows("SELECT * FROM model_usage") == before


def test_reasoning_capture_is_bounded(core, tmp_path):
    _, _, usage, _ = reasoning_chat(core, tmp_path, reasoning="x" * 64010)
    item = CognitionRead(core).page("model", record_id=usage[0]["request_id"])["items"][0]
    assert len(item["reasoning"]["text"]) == 64000
    assert item["reasoning"]["truncated"]


def test_reasoning_read_rejects_concurrent_privacy_change(core, tmp_path, monkeypatch):
    _, _, usage, _ = reasoning_chat(core, tmp_path)
    view = CognitionRead(core)
    original = view._record

    def changed(*args):
        result = original(*args)
        with core.store.transaction(control_write=True):
            advance_epoch(core.store)
        return result

    monkeypatch.setattr(view, "_record", changed)
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        view.page("model", record_id=usage[0]["request_id"])


@pytest.mark.parametrize("corruption", ["invalid_json", "wrong_conversation", "duplicate_response"])
def test_unverifiable_reasoning_snapshot_does_not_expose_text(core, tmp_path, corruption):
    _, job, usage, _ = reasoning_chat(core, tmp_path)
    raw = core.store.one('SELECT conversation_json FROM agent_conversations WHERE job_id=%s',
                         (job["job_id"],))["conversation_json"]
    value = json.loads(raw)
    if corruption == "wrong_conversation":
        value["conversation_id"] = "another-job"
    elif corruption == "duplicate_response":
        value["messages"].append(value["messages"][-1])
    core.store.execute('UPDATE agent_conversations SET conversation_json=%s WHERE job_id=%s',
                       ("{" if corruption == "invalid_json" else json.dumps(value), job["job_id"]))
    item = CognitionRead(core).page("model", record_id=usage[0]["request_id"])["items"][0]
    assert item["reasoning"] == {"state": "INCONSISTENT"}

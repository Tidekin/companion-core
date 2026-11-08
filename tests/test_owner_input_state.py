"""Original signed input remains inspectable without reviving stopped work."""

import pytest

from companion import owner_authority, owner_input, owner_input_state
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop
from companion.store import encode
from companion_onebot_bridge.owner_handoff import terminal
from tests.postgres_fixture import logical_state
from tests.support.owner_input import captured, inbound


def test_discovered_link_can_finish_handoff_through_native_http(core, tmp_path, monkeypatch):
    import json

    from pydantic import SecretStr

    from companion import reply_revision
    from tests.conftest import run
    from tests.support.exploration import URL
    from tests.support.reading_followup import admit, window

    core.settings.control_token = SecretStr("synthetic-discovered-link-control")

    async def scenario():
        async with window(core, tmp_path, monkeypatch) as (client, config, plan):
            original, _ = await admit(client, core, config, plan, 1, "看看 " + URL)
            item = core.store.one('SELECT * FROM content_items WHERE owner_event_id=%s',
                                  (original["event_id"],))
            assert item["state"] == "WAITING" and item["read_request_id"] is None
            assert core.content.status(original["event_id"])["items"][0]["state"] == "DISCOVERED"
            _, accepted = await admit(client, core, config, plan, 2, "先不读了")
            job = core.store.one('SELECT * FROM jobs WHERE job_id=%s', (accepted["job_id"],))
            reply_revision.remember_sources(core.store, job, [original["event_id"]])
            core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s",
                               (accepted["job_id"],))
            stopped = await client.post("/internal/v1/owner-authority/stop", json={
                "operation_id": "stop-discovered-link", "authority": plan.authority},
                headers={"Authorization": "Bearer synthetic-discovered-link-control"})
            assert stopped.status_code == 200, stopped.text
            saved = core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                                    (owner_input.INPUT + original["event_id"],))
            capture = json.loads(saved["value"])["capture"]
            before = logical_state(core.store)
            response = await client.post("/internal/v1/owner-authority/inputs/state", json=capture)
            assert response.status_code == 200, response.text
            receipt = response.json()
            assert receipt["terminal"] and not receipt["unsettled_work"], receipt
            terminal(receipt, capture)
            assert logical_state(core.store) == before
            assert not core.store.one("SELECT 1 FROM exploration_requests")

    run(scenario())


@pytest.mark.parametrize("change", [
    {}, {"kind": "qq_image"}, {"kind": "link_image"}, {"state": "QUEUED"},
    {"state": "UNKNOWN"}, {"read_request_id": "read-unconfirmed"},
    {"lease_token": "lease-unconfirmed"}, {"lease_until": "2026-01-01T00:00:00Z"},
    {"reserved_bytes": 1}, {"usage_request_id": "model-unconfirmed"},
    {"started_at": "2026-01-01T00:00:00Z"}, {"media_sha256": "a" * 64},
    {"fingerprint": "b" * 64}, {"error_code": "CONTENT_PROCESSOR_LOST"},
])
def test_only_unstarted_native_link_discovery_is_not_pending(core, tmp_path, change):
    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    accepted = core.ingest(inbound(core, capture), "discovered-link-state")
    core.store.execute("UPDATE jobs SET state='CANCELLED' WHERE job_id=%s", (accepted["job_id"],))
    owner_authority.stop(core.store, core.settings, core.clock,
        AuthorityStop(operation_id="stop-discovery-proof", authority=capture.authority))
    now = core.clock()
    core.store.insert("content_items", **{
        "item_id": "content-discovered", "owner_event_id": capture.event_id,
        "kind": "link", "slot": 0, "target_url": "https://example.com/source",
        "state": "WAITING", "captured_privacy_epoch": 0,
        "deadline": now, "created_at": now, "updated_at": now, **change})
    before = logical_state(core.store)
    result = owner_input_state.read(core.store, core.settings, capture)
    assert result["terminal"] is (not change)
    assert result["unsettled_work"] is bool(change)
    assert logical_state(core.store) == before


def test_native_input_state_requires_original_capture_and_terminal_work(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    source = core.ingest(inbound(core, capture), "input-state-source")
    pending = owner_input_state.read(core.store, core.settings, capture)
    assert not pending["terminal"] and pending["unsettled_work"]
    assert pending["reply_revision"] == 1
    core.store.execute('UPDATE events SET reply_revision=3 WHERE event_id=%s', (capture.event_id,))
    revised = owner_input_state.read(core.store, core.settings, capture)
    assert revised["reply_revision"] == 3
    assert revised["work_sha256"] == pending["work_sha256"]
    owner_authority.stop(core.store, core.settings, core.clock,
        AuthorityStop(operation_id="stop-input-state", authority=capture.authority))
    core.store.execute("UPDATE jobs SET state='CANCELLED' WHERE job_id=%s", (source["job_id"],))
    result = owner_input_state.read(core.store, core.settings, capture)
    assert result["terminal"] and result["authority"] == capture.authority
    assert result["input_sha256"] == capture.input_sha256
    terminal(result, capture.model_dump(mode="json"))
    historical = {key: value for key, value in result.items() if key != "reply_revision"}
    terminal(historical, capture.model_dump(mode="json"))
    for revision in (-1, True, "3", None):
        with pytest.raises(ValueError, match="OWNER_INPUT_TERMINAL_REQUIRED"):
            terminal({**result, "reply_revision": revision}, capture.model_dump(mode="json"))
    changed = capture.model_copy(update={"input_sha256": "f" * 64})
    with pytest.raises(CoreError, match="OWNER_CAPTURE_INVALID"):
        owner_input_state.read(core.store, core.settings, changed)


def test_unadmitted_stopped_input_has_zero_revision_without_making_work(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    owner_authority.stop(core.store, core.settings, core.clock,
        AuthorityStop(operation_id="stop-unadmitted-input", authority=capture.authority))
    before = logical_state(core.store)
    result = owner_input_state.read(core.store, core.settings, capture)
    assert result["reply_revision"] == 0 and result["state"] == "CANCELLED"
    terminal(result, capture.model_dump(mode="json"))
    assert logical_state(core.store) == before


def test_native_input_state_never_treats_unknown_delivery_as_complete(core, tmp_path):
    from tests.support.owner_delivery_reconciliation import pending

    proof, _ = pending(core, tmp_path)
    import json

    from companion.owner_authority_protocol import OwnerCapture

    saved = core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                           (owner_input.INPUT + proof["event_id"],))
    capture = OwnerCapture.model_validate(json.loads(saved["value"])["capture"])
    result = owner_input_state.read(core.store, core.settings, capture)
    assert not result["terminal"] and result["unsettled_delivery"]


@pytest.mark.parametrize("kind", ["content", "reader", "video", "voice", "cleanup"])
def test_unknown_or_unconfirmed_source_work_cannot_authorize_handoff(core, tmp_path, kind):
    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    accepted = core.ingest(inbound(core, capture), "unknown-source-work")
    core.store.execute("UPDATE jobs SET state='CANCELLED' WHERE job_id=%s", (accepted["job_id"],))
    event = core.store.one('SELECT * FROM events WHERE event_id=%s', (capture.event_id,))
    now = core.clock()
    if kind in {"content", "reader", "video"}:
        core.store.insert("exploration_requests",
            request_id="read-original", request_key="read-key",
            adapter_id="bili", operation="read", url="https://example.com/source",
            state="UNKNOWN" if kind == "reader" else "DONE", captured_privacy_epoch=0,
            created_at=now, updated_at=now, reserved_bytes=100, used_bytes=40)
        core.store.insert("content_items", item_id="content-original",
            owner_event_id=capture.event_id,
            kind="link", slot=0, read_request_id="read-original", target_url="https://example.com/source",
            state="UNKNOWN" if kind == "content" else "SUCCEEDED", captured_privacy_epoch=0,
            deadline=now, created_at=now, updated_at=now, reserved_bytes=100)
        if kind == "video":
            core.store.insert("bili_video_items",
                item_id="content-original", request_id="read-original",
                source_event_id=capture.event_id, owner_event_id=capture.event_id,
                job_id=accepted["job_id"], adapter_id="bili", source_sha256="a" * 64,
                source_snapshot_sha256="b" * 64, metadata_json="{}", source_grant_sha256="c" * 64,
                policy_sha256="d" * 64, window_sha256="", binding_sha256="e" * 64,
                captured_privacy_epoch=0, deadline=now, state="UNKNOWN",
                created_at=now, updated_at=now)
    else:
        core.store.insert("voice_jobs", job_id="voice-original", operation="asr",
            event_id=capture.event_id, native_source_event_id=event["source_event_id"],
            conversation_id=event["conversation_id"], source_segment_index=0,
            captured_privacy_epoch=0, captured_reply_revision=event["reply_revision"],
            node_id="local", config_json="{}", state="UNKNOWN" if kind == "voice" else "CANCELLED",
            input_mime="audio/wav", source_partial=0,
            created_at=now, updated_at=now, deadline_at=now,
            content_retired=1, cleanup_state="CONFIRMED" if kind == "voice" else "PENDING",
            authority_json=encode(capture.authority))
    before = {table: core.store.rows(f"SELECT * FROM {table}") for table in (
        "content_items", "exploration_requests", "bili_video_items", "voice_jobs", "model_usage")}
    result = owner_input_state.read(core.store, core.settings, capture)
    assert result["unsettled_work"] and not result["terminal"]
    with pytest.raises(ValueError, match="OWNER_INPUT_TERMINAL_REQUIRED"):
        terminal(result, capture.model_dump(mode="json"))
    assert before == {table: core.store.rows(f"SELECT * FROM {table}") for table in before}
    if kind == "cleanup":
        core.store.execute("UPDATE voice_jobs SET cleanup_state='CONFIRMED'")
        assert owner_input_state.read(core.store, core.settings, capture)["terminal"]


@pytest.mark.parametrize("read_state", ["DONE", "CANCELLED", "FAILED", "QUEUED", "RUNNING",
                                        "UNKNOWN"])
def test_native_agent_read_link_is_judged_by_its_exploration_request(core, tmp_path,
                                                                      read_state):
    # Production 2026-10-29: the agent's read finished (DONE/CANCELLED) but the link row
    # stays WAITING by design, which used to block every STOP -> resume indefinitely.
    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    accepted = core.ingest(inbound(core, capture), "agent-read-link-state")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (accepted["job_id"],))
    owner_authority.stop(core.store, core.settings, core.clock,
        AuthorityStop(operation_id="stop-agent-read-link", authority=capture.authority))
    now = core.clock()
    core.store.insert("exploration_requests", request_id="read-agent", request_key="read-key",
        adapter_id="bili", operation="read", url="https://example.com/source",
        state=read_state, captured_privacy_epoch=0, created_at=now, updated_at=now)
    core.store.insert("content_items", item_id="content-agent-read",
        owner_event_id=capture.event_id, kind="link", slot=0, read_request_id="read-agent",
        target_url="https://example.com/source", state="WAITING", captured_privacy_epoch=0,
        deadline=now, created_at=now, updated_at=now)
    result = owner_input_state.read(core.store, core.settings, capture)
    finished = read_state in {"DONE", "CANCELLED", "FAILED"}
    assert result["unsettled_work"] is not finished
    assert result["terminal"] is finished


@pytest.mark.parametrize("erasure", [True, False])
def test_a_reply_cleared_by_a_later_erasure_keeps_the_input_state_readable(core, tmp_path,
                                                                           erasure):
    # Production 2026-10-30: forgetting one memory cleared every earlier reply, and every
    # current input state then failed OWNER_DELIVERY_BINDING_REQUIRED, blocking handoff.
    import json

    from companion import transport
    from companion.owner_authority_protocol import OwnerCapture
    from tests.support.owner_delivery_reconciliation import pending

    proof, _ = pending(core, tmp_path)
    saved = core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                           (owner_input.INPUT + proof["event_id"],))
    capture = OwnerCapture.model_validate(json.loads(saved["value"])["capture"])
    before = owner_input_state.read(core.store, core.settings, capture)
    core.clock.advance(1)
    if erasure:
        core.store.execute('INSERT INTO privacy_erasure_ledger VALUES (%s,%s,%s,%s,%s,%s)',
                           ("erase_test", "memory", "mem_test", core.clock(), 1, "COMPLETE"))
    transport.revoke_deliveries(core.store, "TRUE", erase_content=True)
    if erasure:
        after = owner_input_state.read(core.store, core.settings, capture)
        assert after["terminal"] == before["terminal"]
    else:
        with pytest.raises(CoreError, match="OWNER_DELIVERY_BINDING_REQUIRED"):
            owner_input_state.read(core.store, core.settings, capture)

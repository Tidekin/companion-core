"""Read-side frozen REM records; this fixture does not claim REM execution acceptance."""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from companion import api, rem
from companion.cognition_read import CognitionRead
from companion.contracts import CoreError
from companion.privacy import advance_epoch
from companion.store import after, encode
from tests.support.confirmed_memory import remember


def retained_record(core, number=1):
    """Use real Memory evidence/proof and the frozen persisted-job/usage schema."""
    mid = remember(core, number, f"合成主题{number}喜欢绿色", confirmed=True)
    memory = core.store.one('SELECT * FROM memories WHERE memory_id=%s', (mid,))
    memory["evidence_ids"] = [f"evt_test_{number}"]
    proof = core.memory_context.pin(core.clock(), rows=[memory])
    job = core.new_job(f"evt_test_{number}", "rem", f"retained-rem-{number}", core.clock())
    request = f"rem_request_{number}"
    core.store.insert("model_usage", usage_id=f"rem_usage_{number}", request_id=request,
                      job_id=job, scenario="rem", provider="synthetic", model="synthetic",
                      state="UNKNOWN", input_tokens=120, output_tokens=50,
                      cached_input_tokens=20, billed_micro_usd=700, occurred_at=core.clock())
    rid = f"rem_fixture_{number}"
    core.store.insert("rem_records", rem_id=rid, job_id=job,
                      local_date=f"2026-10-{number:02d}", timezone=core.settings.timezone,
                      config_sha256="a" * 64, source_memory_ids_json=encode([mid]),
                      source_event_ids_json=encode(proof["source_event_ids"]),
                      source_snapshot_json=encode({"inputs": [], "proof": proof}),
                      output_json=encode({"kind": "FICTION", "decision": "DREAM",
                          "title": "合成私人标题", "text": "合成私人虚构正文",
                          "questions": [{"text": "未验证的问题？", "state": "UNVERIFIED"}]}),
                      model_request_id=request, created_at=core.clock(),
                      expires_at=after(core.clock(), 86400))
    return rid, job


def test_rem_native_metadata_pagination_and_usage(core):
    first, _ = retained_record(core, 1)
    second, _ = retained_record(core, 2)
    before = core.store.db.total_changes
    view = CognitionRead(core)
    page = view.page("rem", limit=1)
    assert page["items"][0]["id"] == second and page["truncated"]
    older = view.page("rem", limit=1, before=page["next_cursor"])
    assert older["items"][0]["id"] == first and not older["truncated"]
    item = view.page("rem", record_id=first)["items"][0]
    original = next(row for row in rem.records(core) if row["rem_id"] == first)
    assert item["usage"] == original["usage"]
    assert item["usage"]["state"] == "UNKNOWN" and item["usage"]["billed_micro_usd"] == 700
    assert item["kind"] == "FICTION" and item["audience"] == "owner_only"
    assert item["occurred_at"] == core.store.one(
        'SELECT created_at FROM rem_records WHERE rem_id=%s', (first,))["created_at"]
    assert item["questions"] == {"state": "UNVERIFIED", "count": 1}
    assert item["source_available"]
    assert "合成私人" not in json.dumps(item, ensure_ascii=False)
    assert "未验证的问题" not in json.dumps(item, ensure_ascii=False)
    assert core.store.db.total_changes == before
    assert view.page("rem", record_id="absent")["record_status"] == "NOT_RECORDED"
    assert core.model.calls == []


@pytest.mark.parametrize("change", ["record_expiry", "source_expiry", "erasure", "scrub"])
def test_rem_unavailable_sources_hide_refs_but_preserve_accounting(core, change):
    rid, _ = retained_record(core)
    if change == "record_expiry":
        core.clock.advance(86401)
    elif change == "source_expiry":
        core.settings.raw_event_retention_days = 1
        core.store.execute('UPDATE rem_records SET expires_at=%s', (after(core.clock(), 300000),))
        core.clock.advance(86401)
    elif change == "erasure":
        with core.store.transaction():
            core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    else:
        with core.store.transaction():
            rem.erase(core.store, ["evt_test_1"])
    item = CognitionRead(core).page("rem", record_id=rid)["items"][0]
    assert not item["source_available"]
    assert item["source_memory_ids"] == item["source_event_ids"] == []
    assert item["decision"] == "NOT_AVAILABLE"
    assert item["usage"]["state"] == "UNKNOWN" and item["usage"]["billed_micro_usd"] == 700
    assert core.store.one("SELECT COUNT(*) n FROM model_usage")["n"] == 1
    if change == "scrub":
        assert item["state"] == "CANCELLED" and item["last_error_code"] == "REM_SOURCE_ERASED"


def test_rem_privacy_race_rejects_entire_page(core, monkeypatch):
    retained_record(core)
    original = core.memory_context.revalidate

    def changed(*args):
        original(*args)
        advance_epoch(core.store)

    monkeypatch.setattr(core.memory_context, "revalidate", changed)
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        CognitionRead(core).page("rem")


def test_rem_source_reference_cannot_disagree_with_saved_proof(core):
    rid, _ = retained_record(core)
    core.store.execute((
        'UPDATE rem_records SET source_event_ids_json=\'["different"]\' WHERE '
        'rem_id=%s'
    ), (rid,))
    item = CognitionRead(core).page("rem", record_id=rid)["items"][0]
    assert not item["source_available"] and item["source_event_ids"] == []
    assert item["usage"]["billed_micro_usd"] == 700


def test_rem_real_application_route_requires_owner(core):
    core.clock.now = datetime.now(UTC)
    rid, _ = retained_record(core)
    core.settings.control_token = SecretStr("synthetic-rem-owner-token")
    app = api.create_app(core.settings)
    app.state.core = core
    with TestClient(app) as client:
        # Startup has its own Core; use the actual app route and persisted DB.
        assert app.state.core is not core
        for headers in ({}, {"Authorization": "Bearer " +
                             core.settings.bridge_token.get_secret_value()}):
            assert client.post("/internal/v2/cognition", json={"kind": "rem"},
                               headers=headers).status_code == 401
        response = client.post("/internal/v2/cognition", json={"kind": "rem", "record_id": rid},
            headers={"Authorization": "Bearer synthetic-rem-owner-token"})
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["items"][0]["content_kind"] == "private_fiction"
        assert response.json()["items"][0]["source_available"]

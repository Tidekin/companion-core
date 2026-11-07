"""Readback integrity of a real synthetic native-worker QZone review artifact."""

import copy
import json

import httpx
import pytest

from companion import owner_authority, paid_budget
from companion.life_protocol import LifeQZoneDraft
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.store import encode
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.life_worker_integration import check_independent_life_worker as produce


def test_tampered_or_revoked_artifacts_are_hidden_individually_without_readback_writes(
    core,
    tmp_path,
    monkeypatch,
):
    produce(core, tmp_path, monkeypatch, "qzone")
    original = core.store.one("SELECT * FROM life_activity_results")
    proposal = json.loads(original["proposal_json"])
    draft = LifeQZoneDraft.model_validate(proposal["qzone_draft"])
    assert draft.content_sha256 == paid_budget.sha({"text": draft.text})
    assert draft.source_snapshot_sha256 == paid_budget.sha(
        {g.event_id: g.source_sha256 for g in draft.sources}
    )
    source = core.store.one("SELECT * FROM events WHERE event_id=?", (draft.sources[0].event_id,))
    usage = core.store.rows("SELECT * FROM model_usage")
    jobs = core.store.rows("SELECT * FROM jobs")
    headers = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
    observations = []

    async def exercise(client):
        assert (await client.get("/internal/v2/life/topics")).status_code == 401
        for case in (
        "valid",
        "text_tampered",
        "content_hash_tampered",
        "source_hash_tampered",
        "duplicate_sources",
        "audience_tampered",
        "owner_tampered",
        "purpose_tampered",
        "grant_revision_tampered",
        "expired_draft",
        "extended_expiry",
        "missing_draft",
        "wrong_draft_id",
        "boolean_version",
        "malformed_json",
        "changed_retained_text",
        "source_changed",
        ):
            await check_case(client, case)

    async def check_case(client, case):
        value = copy.deepcopy(proposal)
        nested = value["qzone_draft"]
        core.store.execute(
            "UPDATE events SET body_json=?,redacted=0 WHERE event_id=?",
            (source["body_json"], source["event_id"]),
        )
        core.store.execute("UPDATE life_activity_results SET text=?", (original["text"],))
        if case == "text_tampered":
            nested["text"] = "篡改后的合成正文"
        elif case == "content_hash_tampered":
            nested["content_sha256"] = "0" * 64
        elif case == "source_hash_tampered":
            nested["source_snapshot_sha256"] = "0" * 64
        elif case == "duplicate_sources":
            nested["sources"].append(copy.deepcopy(nested["sources"][0]))
        elif case == "audience_tampered":
            nested["audience"] = "public"
        elif case == "owner_tampered":
            nested["owner_qq_id"] = "99999"
        elif case == "purpose_tampered":
            nested["purpose"] = "qzone_publish"
        elif case == "grant_revision_tampered":
            nested["sources"][0]["revision"] = 99
        elif case in {"expired_draft", "extended_expiry"}:
            nested["expires_at"] = (
                "2020-01-01T00:00:00Z" if case == "expired_draft" else "2030-01-01T00:00:00Z"
            )
        elif case == "missing_draft":
            del value["qzone_draft"]
        elif case == "wrong_draft_id":
            nested["draft_id"] = "job_unrelated"
        elif case == "boolean_version":
            nested["schema_version"] = True
        elif case == "changed_retained_text":
            core.store.execute("UPDATE life_activity_results SET text='改写存储正文'")
        elif case == "source_changed":
            core.store.execute(
                "UPDATE events SET body_json=? WHERE event_id=?",
                (encode({"text": "变更后的合成来源"}), source["event_id"]),
            )
        core.store.execute(
            "UPDATE life_activity_results SET proposal_json=?",
            ("{" if case == "malformed_json" else encode(value),),
        )
        before = core.store.db.total_changes
        response = await client.get("/internal/v2/life/topics", headers=headers)
        visible = (
            []
            if response.status_code != 200
            else [result for topic in response.json()["items"] for result in topic["results"]]
        )
        observations.append(
            {
                "case": case,
                "status": response.status_code,
                "visible": bool(visible),
                "read_only": before == core.store.db.total_changes,
            }
        )
        assert core.store.rows("SELECT * FROM outbox") == []
        assert core.store.rows("SELECT * FROM social_autonomy_plans") == []
        assert core.store.rows("SELECT * FROM model_usage") == usage
        assert core.store.rows("SELECT * FROM jobs") == jobs

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
        ) as client:
            await exercise(client)

    run(scenario())
    assert all(
        row["status"] == 200 and row["read_only"] and row["visible"] == (row["case"] == "valid")
        for row in observations
    ), observations


@pytest.mark.parametrize("change", [
    "grant_revoked", "grant_revised", "source_withdrawn", "natural_expiry", "unknown",
])
def test_native_revocation_and_unknown_preserve_readback_accounting(
    core, tmp_path, monkeypatch, change,
):
    produce(core, tmp_path, monkeypatch, "unknown" if change == "unknown" else "qzone")
    original = core.store.one("SELECT * FROM life_activity_results")
    if change != "unknown":
        view = owner_authority.current(core.store, core.settings, core.clock)
        source_id = view.policy.life_shadow.source_event_ids[0]
    usage = core.store.rows("SELECT * FROM model_usage")

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + core.settings.control_token.get_secret_value()},
        ) as client:
            if change.startswith("grant_"):
                scope = view.policy.life_shadow
                grants = ([] if change == "grant_revoked" else [
                    scope.qzone_draft_sources[0].model_copy(update={"revision": 2})])
                policy = OwnerGrantPolicy.model_validate({**view.policy.model_dump(),
                    "life_shadow": scope.model_copy(update={"qzone_draft_sources": grants})})
                body = {"expected_revision": view.authority.revision,
                        "policy": policy.model_dump(mode="json")}
                preview = await client.post("/internal/v1/owner-authority/preview", json=body)
                assert preview.status_code == 200, preview.text
                activated = await client.post("/internal/v1/owner-authority/activate", json={
                    **body, "operation_id": "qzone-readback-" + change,
                    "preview_sha256": preview.json()["preview_sha256"]})
                assert activated.status_code == 200, activated.text
                assert activated.json()["authority"]["revision"] > view.authority.revision
            elif change == "source_withdrawn":
                with core.store.transaction():
                    core.ledger.expire_events(core.store, [source_id], core.clock())
                assert core.store.one("SELECT redacted FROM events WHERE event_id=?",
                                      (source_id,))["redacted"]
            elif change == "natural_expiry":
                core.clock.advance(86401)
            jobs = core.store.rows("SELECT * FROM jobs")
            changes = core.store.db.total_changes
            response = await client.get("/internal/v2/life/topics")
            assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
            visible = [row for topic in response.json()["items"] for row in topic["results"]]
            if change == "unknown":
                assert not any(row["state"] == "SHADOW" or row.get("qzone_draft")
                               for row in visible)
                assert any(row["job_id"] == original["job_id"] and row["state"] == "UNKNOWN"
                           and row["billed_micro_usd"] > 0 for row in usage)
            else:
                assert not visible
            assert core.store.db.total_changes == changes
            assert core.store.rows("SELECT * FROM jobs") == jobs
            assert core.store.rows("SELECT * FROM model_usage") == usage
            assert core.store.rows("SELECT * FROM outbox") == []
            assert core.store.rows("SELECT * FROM social_autonomy_plans") == []
    run(scenario())

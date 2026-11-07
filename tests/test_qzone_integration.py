"""Signed owner HTTP -> native target -> isolated Reader -> source-bound Life draft."""

import json
from contextlib import closing
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet

from companion import owner_authority
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.life_protocol import LifeLoopConfig, LifeQZoneSourceGrant, LifeShadowApproval
from companion.life_topics import _source, authority
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.qzone import select_target
from companion.qzone_protocol import QZoneTarget
from companion.reading_protocol import ReadObservation
from companion.social_autonomy import public_source
from companion.store import Store
from source_reader.qzone import QZoneHTTP, QZoneReader
from source_reader.reading import ReadingContext, read_observation
from tests.conftest import run
from tests.support.life_worker_integration import process_worker
from tests.support.native_consumer_entry import environment
from tests.support.owner_authority import activate
from tests.support.qzone_integration import options, reader_process, selected
from tests.support.qzone_reader import TOKEN, URL, account_file, pool_for, response


def test_real_http_target_and_independent_reader_are_idempotent_and_owner_only(core, tmp_path):
    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "content", "social"),
                               core_options=options()) as (entry, sends):
            event, control, account, grant, request_id, body = await selected(core, entry, tmp_path)
            assert (await entry.client.get("/internal/v2/qzone/targets")).status_code == 401
            changed = {**body, "url": "https://user.qzone.qq.com/99999999"}
            conflict = await entry.client.post("/internal/v2/qzone/targets", headers=control,
                                               json=changed)
            assert conflict.status_code == 409
            with core.store.transaction(), pytest.raises(
                CoreError, match="QZONE_TARGET_SELECTION_REQUIRED",
            ):
                core.exploration.create("qzone", "read", "model-selected-target", URL,
                                        source_event_ids=[event["event_id"]])
            other = {**grant, "grant_id": "grant_" + "f" * 32}
            assert (await entry.client.post("/internal/v2/source-grants", headers=control,
                json={"adapter_id": "qzone", **other})).status_code == 201
            wrong = await entry.client.post("/internal/v3/readers/qzone/lease",
                headers={"Authorization": "Bearer " + TOKEN}, json={"source_grant": other})
            assert wrong.status_code == 200 and wrong.json()["item"] is None
            assert not core.store.rows("SELECT * FROM reading_attempts")
            first = await reader_process(core, entry.client, account)
            assert first["calls"] and first["calls"][0]["method"] == "GET"
            assert (await reader_process(core, entry.client, account))["calls"] == []
            attempt = core.store.one('SELECT * FROM reading_attempts WHERE request_id=%s',
                                     (request_id,))
            assert attempt["state"] == "DONE" and attempt["observed_requests"] == 1
            sources = json.loads(attempt["result_event_ids_json"])
            assert len(sources) == 1
            source_id = sources[0]
            shown = await entry.client.get("/internal/v2/qzone/sources/" + source_id,
                                           headers=control)
            assert shown.status_code == 200 and shown.json()["tid"] == "synthetic-tid-one"
            assert shown.headers["Cache-Control"] == "no-store"
            assert "PRIVATE_OWNER_SENTINEL" not in shown.text
            assert "UNREQUESTED_COMMENT" not in shown.text
            targets = await entry.client.get("/internal/v2/qzone/targets", headers=control)
            assert targets.json()["items"][0]["event_ids"] == sources
            with pytest.raises(CoreError, match="SOCIAL_PUBLIC_SOURCE_REQUIRED"):
                public_source(core.store, core.settings, source_id, core.clock())
            assert core.social.preview(source_id, core.settings.owner_qq_id) is None
            assert not sends and not core.store.rows("SELECT * FROM outbox")
            assert not core.store.rows("SELECT * FROM model_usage")
            assert not core.store.rows("SELECT * FROM memories")
            # Future-read revocation keeps authorized history available for review.
            revoked = await entry.client.post("/internal/v2/source-grants/"
                + grant["grant_id"] + "/revoke", headers=control)
            assert revoked.status_code == 200
            assert (await entry.client.get("/internal/v2/qzone/sources/" + source_id,
                                            headers=control)).status_code == 200
            forgotten = await entry.client.post("/internal/v2/source-grants/"
                + grant["grant_id"] + "/forget", headers=control)
            assert forgotten.status_code == 200
            assert (await entry.client.get("/internal/v2/qzone/sources/" + source_id,
                                            headers=control)).status_code == 403
            assert not (await entry.client.get("/internal/v2/qzone/targets",
                                               headers=control)).json()["items"]

    run(scenario())


@pytest.mark.parametrize("change", ["revoke", "forget", "stop", "source_recall", "disabled",
                                   "revision"])
def test_late_receipt_never_revives_content_or_releases_observed_usage(core, tmp_path, change):
    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "content", "social"),
                               core_options=options()) as (entry, sends):
            _, control, account, grant, request_id, _ = await selected(core, entry, tmp_path)
            adapter = {"Authorization": "Bearer " + TOKEN}
            leased = await entry.client.post("/internal/v3/readers/qzone/lease", headers=adapter,
                                              json={"source_grant": grant})
            assert leased.status_code == 200, leased.text
            item = leased.json()["item"]

            async def guard(context):
                return None

            async with QZoneHTTP(account, pool_for(response(core.clock.now), []),
                                  lambda: core.clock.now) as public:
                context = ReadingContext(**{**item["context"], "source_event_ids": tuple(
                    item["context"]["source_event_ids"])})
                value = await read_observation(QZoneReader(public, lambda: core.clock.now),
                    item, context, guard, clock=core.clock)
            result = ReadObservation.model_validate({**value, "lease_token": item["lease_token"],
                                                     "source_grant": grant})
            if change in {"revoke", "forget"}:
                assert (await entry.client.post("/internal/v2/source-grants/"
                    + grant["grant_id"] + "/" + change, headers=control)).status_code == 200
            elif change == "disabled":
                core.settings.qzone_reading_enabled = False
            elif change == "revision":
                revised = {**grant, "epoch": 2, "fingerprint": "b" * 64}
                assert (await entry.client.post("/internal/v2/source-grants", headers=control,
                    json={"adapter_id": "qzone", **revised})).status_code == 201
            elif change == "source_recall":
                await entry.recall(1)
            else:
                await entry.stop()
            prefix = "/internal/v3/readers/qzone/" + request_id
            checked = await entry.client.post(prefix + "/validate", headers=adapter,
                json={"lease_token": item["lease_token"], "source_grant": grant})
            assert checked.status_code != 200
            completed = await entry.client.post(prefix + "/result", headers=adapter,
                                                json=result.model_dump(mode="json"))
            assert completed.status_code == 200, completed.text
            assert completed.json()["state"] == "CANCELLED"
            assert not core.store.rows("SELECT * FROM observations")
            row = core.store.one(
                'SELECT used_requests FROM exploration_requests WHERE request_id=%s', (request_id,))
            assert row["used_requests"] == 1
            assert not sends and not core.store.rows("SELECT * FROM outbox")

    run(scenario())


def test_default_configuration_and_unapproved_runtime_cannot_admit_qzone(core, tmp_path):
    grant = account_file(tmp_path / "grant.json", core.clock.now)
    body = QZoneTarget(operation_id="unapproved-qzone", adapter_id="qzone", url=URL,
                       source_event_id="evt_unapproved", source_grant=grant)
    assert not core.settings.qzone_reading_enabled
    with pytest.raises(CoreError, match="QZONE_READING_DISABLED"):
        select_target(core.reading, body)
    core.settings.qzone_reading_enabled = True
    with pytest.raises(CoreError, match="OWNER_AUTHORITY"):
        select_target(core.reading, body)
    assert not core.store.rows("SELECT * FROM exploration_requests")


def test_expired_queued_target_never_reserves_a_read(core, tmp_path):
    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "content", "social"),
                               core_options=options()) as (entry, sends):
            _, _, _, grant, request_id, _ = await selected(core, entry, tmp_path)
            core.clock.advance(301)
            leased = await entry.client.post("/internal/v3/readers/qzone/lease",
                headers={"Authorization": "Bearer " + TOKEN}, json={"source_grant": grant})
            assert leased.status_code == 200 and leased.json()["item"] is None
            assert not core.store.rows("SELECT * FROM reading_attempts")
            row = core.store.one((
                'SELECT state,last_error_code,started_at,lease_token FROM '
                'exploration_requests WHERE request_id=%s'
            ), (request_id,))
            assert row["state"] == "CANCELLED" and row["last_error_code"] == "QZONE_TARGET_EXPIRED"
            assert row["started_at"] is None and row["lease_token"] is None and not sends

    run(scenario())


def test_space_draft_uses_only_space_sources_and_native_life_worker(core, tmp_path):
    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "content", "social"),
                               core_options=options()) as (entry, sends):
            event, control, account, grant, _, _ = await selected(core, entry, tmp_path)
            await reader_process(core, entry.client, account)
            source_id = core.store.one("SELECT event_id FROM observations")["event_id"]
            shown = await entry.client.get("/internal/v2/qzone/sources/" + source_id,
                                           headers=control)
            assert shown.status_code == 200, shown.text
            view = owner_authority.current(core.store, core.settings, core.clock)
            stopped = await entry.client.post("/internal/v1/owner-authority/stop", headers=control,
                json={"authority": view.authority.as_dict(), "operation_id": "qzone-read-complete"})
            assert stopped.status_code == 200, stopped.text
            entry.runtime.stop()
            core.settings.life_loop = LifeLoopConfig(mode="SHADOW")
            core.settings.wake_enabled = True
            conversation = core.store.one('SELECT conversation_id FROM events WHERE event_id=%s',
                                          (event["event_id"],))["conversation_id"]
            draft_grant = LifeQZoneSourceGrant(grant_id="synthetic-qzone-draft-grant", revision=1,
                purpose="qzone_draft", event_id=source_id,
                source_sha256=shown.json()["source_sha256"],
                expires_at=core.clock.now + timedelta(hours=1), audience="owner_only",
                owner_qq_id=core.settings.owner_qq_id)
            scope = LifeShadowApproval(scope="owner_private_life_shadow_v1",
                authorization_sha256="b" * 64, conversation_id=conversation,
                source_event_ids=[source_id], purposes=[],
                qzone_draft_sources=[draft_grant], max_items=1,
                expires_at=core.clock.now + timedelta(hours=1))
            policy = OwnerGrantPolicy.model_validate({**view.policy.model_dump(),
                "purposes": [*view.policy.purposes, "life"], "life_shadow": scope})
            activate(core, policy, revision=stopped.json()["authority"]["revision"],
                     operation="qzone-review-draft-scope")
            space_source = _source(core.store, core.settings, source_id, core.clock())
            assert space_source["source_sha256"] == shown.json()["source_sha256"]
            private = {**space_source, "kind": "owner_message"}
            with pytest.raises(CoreError, match="LIFE_SOCIAL_DRAFT_SCOPE_REQUIRED"):
                authority(core.store, core.settings, conversation, [space_source, private],
                          core.clock())
            core.clock.advance(1)
            observed = await process_worker(core, tmp_path, "qzone", source_id)
            assert observed["provider_calls"] == 1
            material = observed["materials"][0]
            assert material["allowed_destinations"] == ["QZONE_DRAFT"]
            assert material["decision_context"] == {
                "owner_feedback": [], "self_interests": [], "execution_outcomes": []}
            assert "PRIVATE_OWNER_SENTINEL" not in json.dumps(material)
            assert material["read_targets"] == [] and not material["read_goals_enabled"]
            result = core.store.one("SELECT * FROM life_activity_results")
            assert result["state"] == "SHADOW" and result["destination"] == "QZONE_DRAFT"
            assert not sends and not core.store.rows("SELECT * FROM outbox")
            assert not core.store.rows("SELECT * FROM social_autonomy_plans")
            assert not core.store.rows("SELECT * FROM memories")
            usage = core.store.one('SELECT * FROM model_usage WHERE job_id=%s',
                                   (result["job_id"],))
            key = Fernet.generate_key()
            archive, restored_path = tmp_path / "qzone-before-forget.enc", tmp_path / "restored.db"
            backup(core.settings.database, archive, key)
            forgotten = await entry.client.post("/internal/v2/source-grants/"
                + grant["grant_id"] + "/forget", headers=control)
            assert forgotten.status_code == 200
            assert core.store.one("SELECT text,proposal_json,state FROM life_activity_results") == {
                "text": "", "proposal_json": "{}", "state": "INVALIDATED"}
            restore(archive, restored_path, key, core.settings.erasure_ledger)
            with closing(Store(str(restored_path))) as restored:
                assert restored.one("SELECT body_json,redacted FROM events WHERE event_id=?",
                                    (source_id,)) == {"body_json": "{}", "redacted": 1}
                assert restored.one(
                    "SELECT text,proposal_json,state FROM life_activity_results") == {
                    "text": "", "proposal_json": "{}", "state": "INVALIDATED"}
                retained = restored.one("SELECT * FROM model_usage WHERE request_id=?",
                                        (usage["request_id"],))
                assert retained["state"] == "SETTLED"
                assert retained["billed_micro_usd"] == usage["billed_micro_usd"]
                assert not restored.rows("PRAGMA foreign_key_check")

    run(scenario())

"""Signed Life input and native workers save exact fiction in existing REM custody."""

import hashlib
from contextlib import asynccontextmanager

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr, ValidationError

from companion import budget, owner_authority, paid_budget, rem
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from companion.life_protocol import LifeLoopConfig, LifeShadowApproval
from companion.model import ModelClient
from companion.owner_authority_protocol import AuthorityStop, OwnerCapture, OwnerGrantPolicy
from companion.qq_rich import input_digest
from companion.rem_protocol import RemConfig
from companion.store import after
from companion_delivery_protocol.authority import capture_signing_payload
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.owner_authority import activate
from tests.support.owner_memory import environment
from tests.test_life_expression_consumption import request
from tests.test_life_expression_entrypoint import native_child
from tests.test_owner_recall import receipt
from tests.test_rem_native import response
from tests.test_social_owner_authority import stop


@asynccontextmanager
async def prepared(core, tmp_path, *, automatic=False, purposes=("rem", "memory"),
                   creation=True, enabled=True, capture_key=None):
    core.settings.rem = RemConfig(enabled=enabled, local_hour=0,
                                  daily_micro_usd=50000, monthly_micro_usd=200000)
    core.settings.life_loop = LifeLoopConfig(mode="SHADOW", follow_up_delay_seconds=60)
    core.settings.wake_enabled = True
    core.settings.quiet_start = core.settings.quiet_end = 0
    core.settings.control_token = SecretStr("synthetic-life-rem-control")
    async with environment(core, tmp_path, history=False) as (admit, recall, original):
        capture, _ = await admit(11, "我想把夜空和纸上的花园写成一个小故事。")
        policy = owner_authority.current(core.store, core.settings, core.clock).policy
        # Native STOP cancels pending foreground custody before this explicit new scope.
        stopped = owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
            operation_id="stop-seed-foreground", authority=original))
        conversation = core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
                                      (capture.event_id,))["conversation_id"]
        scope = LifeShadowApproval(
            scope="owner_private_life_shadow_v1", authorization_sha256="c" * 64,
            conversation_id=conversation, source_event_ids=[capture.event_id],
            purposes=["PRIVATE_CREATION"] if creation else ["PRIVATE_DIARY"],
            expires_at=after(core.clock(), 86400), max_items=3,
            automatic_actions=["rem.save"] if automatic else [],
        )
        policy = OwnerGrantPolicy.model_validate({**policy.model_dump(),
            "purposes": ["chat", "life", *purposes], "life_shadow": scope,
            "capture": {**policy.capture.model_dump(),
                        "public_key": capture_key.public_key().public_bytes_raw().hex()}
            if capture_key else policy.capture,
            "memory_scope": "owner_private" if "memory" in purposes else "none",
            "capability_policies": {"rem": paid_budget.sha(core.settings.rem.model_dump())}
            if "rem" in purposes else {}})
        reference = activate(core, policy, revision=stopped["authority"]["revision"],
                             operation="enable-life-private-creation")[0]["authority"]
        core.clock.advance(max(61, core.settings.proactive_idle_seconds + 1))
        yield reference, capture, recall, policy


async def draft(core, tmp_path, capture, *, independent=False):
    if independent:
        outcome = await native_child(core, tmp_path, capture.event_id, "PRIVATE_CREATION", "draft")
        assert outcome["provider_calls"] == 1, outcome
        rows = [row for row in outcome["results"] if row["state"] == "SHADOW"]
        assert len(rows) == 1, outcome
        return rows[0]
    job_id = core.life_topics.schedule()
    assert job_id
    value = {"action": "SHARE", "destination": "PRIVATE_CREATION",
             "source_event_ids": [capture.event_id], "reason": "私人虚构创作",
             "text": "虚构：夜空化成了纸上的花园。"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response(value)),
    ) as client:
        previous = core.model
        core.model = ModelClient(core.settings, client)
        try:
            assert await core.run_once(job_id=job_id)
        finally:
            core.model = previous
    row = core.store.one("SELECT * FROM life_activity_results WHERE job_id=?", (job_id,))
    assert row["state"] == "SHADOW", core.store.rows("SELECT * FROM jobs")
    return row


def test_signed_http_life_to_independent_rem_worker_is_exact_once(core, tmp_path):
    async def scenario():
        async with prepared(core, tmp_path) as (reference, capture, _, _):
            retained = await draft(core, tmp_path, capture, independent=True)
            assert core.store.rows("SELECT * FROM rem_records") == []
            usage = core.store.rows("SELECT * FROM model_usage")
            assert len(usage) == 1 and usage[0]["state"] == "SETTLED"
            unchanged = {table: core.store.rows(f"SELECT * FROM {table}") for table in (
                "memories", "observations", "diary_entries", "goals", "outbox")}
            body = request(core, retained["job_id"], reference, "rem.save").model_dump(mode="json")
            path = f"/internal/v2/life/results/{retained['job_id']}/consume"
            async with real_core_http(core) as port, httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False,
            ) as client:
                assert (await client.post(path, json=body)).status_code == 401
                client.headers["Authorization"] = "Bearer synthetic-life-rem-control"
                wrong = await client.post(path, json={**body, "expected_content_sha256": "0" * 64})
                assert wrong.status_code == 409, wrong.text
                response = await client.post(path, json=body)
                assert response.status_code == 200, response.text
                saved = response.json()
                assert saved["state"] == "QUEUED"
                assert (await client.post(path, json=body)).json() == saved
            outcome = await native_child(core, tmp_path, capture.event_id,
                                         "PRIVATE_CREATION", "save")
            assert outcome["provider_calls"] == 0, outcome
            result = rem.records(core)[0]
            assert result["job_id"] == saved["job_id"] and result["state"] == "SUCCEEDED"
            assert result["kind"] == "FICTION" and result["decision"] == "DREAM"
            assert result["text"] == retained["text"] and result["source_available"]
            assert result["source_memory_ids"] == []
            assert capture.event_id in result["source_event_ids"]
            assert result["usage"] is None and result["questions"] == []
            restarted = await native_child(core, tmp_path, capture.event_id,
                                           "PRIVATE_CREATION", "again")
            assert restarted["provider_calls"] == 0
            assert len(core.store.rows("SELECT * FROM rem_records")) == 1
            assert core.store.rows("SELECT * FROM model_usage") == usage
            assert unchanged == {table: core.store.rows(f"SELECT * FROM {table}")
                                 for table in unchanged}
    run(scenario())


def test_automatic_creation_uses_existing_life_and_rem_selector(core, tmp_path):
    async def scenario():
        async with prepared(core, tmp_path, automatic=True) as (_, capture, _, _):
            retained = await draft(core, tmp_path, capture, independent=True)
            result = rem.records(core)[0]
            assert result["state"] == "SUCCEEDED" and result["text"] == retained["text"]
            assert not core.life_topics.consume_due(diary=core.diary)
            assert rem.schedule_due(core) is None
            assert len(core.store.rows("SELECT * FROM model_usage")) == 1
    run(scenario())


@pytest.mark.parametrize("options", [
    {"purposes": ("memory",)}, {"purposes": ("rem",)}, {"creation": False}, {"enabled": False},
])
def test_diary_or_partial_scope_cannot_grant_creation(core, tmp_path, options):
    async def scenario():
        if options.get("enabled") is False:
            with pytest.raises(CoreError, match="OWNER_AUTHORITY_CAPABILITY_CHANGED"):
                async with prepared(core, tmp_path, **options):
                    raise AssertionError("disabled REM cannot acquire its capability grant")
            return
        async with prepared(core, tmp_path, **options):
            job_id = core.life_topics.schedule()
            assert job_id
            topic = core.store.one("SELECT topic_id FROM life_activity_results WHERE job_id=?",
                                   (job_id,))["topic_id"]
            material = core.life_topics.material(topic)
            assert "PRIVATE_CREATION" not in material["allowed_destinations"]
            assert core.store.rows("SELECT * FROM rem_records") == []
            assert core.store.rows("SELECT * FROM model_usage") == []
    run(scenario())


@pytest.mark.parametrize("fence", ["stop", "recall", "revision", "changed", "unknown"])
def test_queued_creation_rechecks_original_source_authority_and_settlement(core, tmp_path, fence):
    async def scenario():
        async with prepared(core, tmp_path) as (reference, capture, recall, policy):
            retained = await draft(core, tmp_path, capture)
            body = request(core, retained["job_id"], reference, "rem.save")
            queued = core.life_topics.consume(retained["job_id"], body)
            if fence == "stop":
                stop(core, reference)
            elif fence == "recall":
                response = await recall(capture)
                assert response.status_code == 200, response.text
            elif fence == "revision":
                stopped = stop(core, reference)
                activate(core, policy, revision=stopped["authority"]["revision"],
                         operation="revise-rem-queue")
            elif fence == "changed":
                core.store.execute("UPDATE life_activity_results SET text='changed' WHERE job_id=?",
                                   (retained["job_id"],))
            else:
                core.store.execute("UPDATE model_usage SET state='UNKNOWN' WHERE job_id=?",
                                   (retained["job_id"],))
            before = core.store.rows("SELECT * FROM model_usage")
            await core.run_once(job_id=queued["job_id"])
            assert not await core.run_once(job_id=queued["job_id"])
            result = rem.records(core)[0]
            assert result["state"] != "SUCCEEDED" and not result["text"]
            assert core.store.rows("SELECT * FROM model_usage") == before
            assert len(core.store.rows("SELECT * FROM rem_records")) == 1
            with pytest.raises(CoreError):
                core.life_topics.consume(retained["job_id"], body)
    run(scenario())


def test_creation_erasure_replays_after_restore_without_new_payment(core, tmp_path):
    async def scenario():
        async with prepared(core, tmp_path, automatic=True) as (_, capture, recall, _):
            await draft(core, tmp_path, capture)
            assert core.life_topics.consume_due(diary=core.diary)
            job_id = core.store.one("SELECT job_id FROM rem_records")["job_id"]
            assert await core.run_once(job_id=job_id)
            assert rem.records(core)[0]["text"]
            archive, target = tmp_path / "before.enc", tmp_path / "restored.db"
            key = Fernet.generate_key()
            backup(core.settings.database, archive, key)
            before = core.store.rows("SELECT * FROM model_usage")
            response = await recall(capture)
            assert response.status_code == 200, response.text
            restore(archive, target, key, core.settings.erasure_ledger)
            with Core(core.settings.model_copy(update={"database": str(target)}),
                      core.model, core.clock) as restored:
                for instance in (core, restored):
                    result = rem.records(instance)[0]
                    assert result["redacted"] and not result["text"]
                    assert not result["source_event_ids"]
                    assert not await instance.run_once(job_id=result["job_id"])
                    assert instance.store.rows("SELECT * FROM model_usage") == before
    run(scenario())


def test_rem_default_enablement_preserves_explicit_creation_scope():
    assert RemConfig().enabled
    scope = dict(scope="owner_private_life_shadow_v1", authorization_sha256="a" * 64,
                 conversation_id="conversation", source_event_ids=["event"],
                 purposes=["PRIVATE_DIARY"], expires_at="2026-11-01T00:00:00Z", max_items=1)
    with pytest.raises(ValidationError):
        LifeShadowApproval(**scope, automatic_actions=["rem.save"])


def test_signed_pending_input_preempts_queued_save_without_second_payment(core, tmp_path):
    key = Ed25519PrivateKey.generate()

    async def scenario():
        async with prepared(core, tmp_path, capture_key=key) as (reference, source, _, policy):
            retained = await draft(core, tmp_path, source)
            body = request(core, retained["job_id"], reference, "rem.save")
            queued = core.life_topics.consume(retained["job_id"], body)
            native_source = source.source_event_id.rsplit(":", 1)[0] + ":12"
            value = dict(authority=reference, kind="authenticated_onebot_ws",
                source_event_id=native_source,
                event_id="evt_" + hashlib.sha256(native_source.encode()).hexdigest(),
                input_sha256=input_digest({"text": "请先回答我的问题"}),
                frame_sha256="b" * 64, evidence_sha256="c" * 64,
                identity_sha256=policy.capture.identity_sha256,
                protocol_epoch=policy.capture.protocol_epoch,
                occurred_at=core.clock(), received_at=core.clock())
            value["signature"] = key.sign(capture_signing_payload(value)).hex()
            capture = OwnerCapture.model_validate(value)
            before = core.store.rows("SELECT * FROM model_usage")
            async with real_core_http(core) as port, httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False,
                headers={"Authorization": "Bearer "
                         + core.settings.bridge_token.get_secret_value()},
            ) as client:
                accepted = await client.post("/internal/v1/owner-authority/inputs/approve",
                                             json=capture.model_dump(mode="json"))
                assert accepted.status_code == 200, accepted.text
                assert not core.store.one("SELECT 1 FROM events WHERE event_id=?",
                                           (capture.event_id,))
                assert await core.run_once(job_id=queued["job_id"])
                result = rem.records(core)[0]
                assert result["state"] == "RETRY_WAIT" and not result["text"]
                assert core.store.rows("SELECT * FROM model_usage") == before
                recalled = await client.post("/internal/v1/owner-authority/inputs/recall",
                    json=receipt(core, capture, key).model_dump(mode="json"))
                assert recalled.status_code == 200, recalled.text
            core.clock.advance(61)
            assert await core.run_once(job_id=queued["job_id"])
            result = rem.records(core)[0]
            assert result["state"] == "SUCCEEDED" and result["text"] == retained["text"]
            assert core.store.rows("SELECT * FROM model_usage") == before
    run(scenario())


def test_life_creation_model_timeout_retains_unknown_without_rem_or_retry(core, tmp_path):
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic uncertain Life generation", request=request)

    async def scenario():
        async with prepared(core, tmp_path) as (reference, _, _, _):
            job_id = core.life_topics.schedule()
            assert job_id
            async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
                previous = core.model
                core.model = ModelClient(core.settings, client)
                try:
                    assert await core.run_once(job_id=job_id)
                    assert not await core.run_once(job_id=job_id)
                finally:
                    core.model = previous
            assert len(calls) == 1
            usage = core.store.rows("SELECT * FROM model_usage")
            assert len(usage) == 1 and usage[0]["state"] == "UNKNOWN"
            assert not core.life_topics.consume_due(diary=core.diary)
            with pytest.raises(CoreError):
                core.life_topics.consume(job_id, request(core, job_id, reference, "rem.save"))
            assert core.store.rows("SELECT * FROM rem_records") == []
            assert core.store.rows("SELECT * FROM model_usage") == usage
    run(scenario())


def test_retained_creation_refuses_second_paid_admission_and_tampered_readback(core, tmp_path):
    async def scenario():
        async with prepared(core, tmp_path) as (reference, capture, _, _):
            retained = await draft(core, tmp_path, capture)
            queued = core.life_topics.consume(retained["job_id"],
                request(core, retained["job_id"], reference, "rem.save"))
            job = core.claim_job("rem-save-test", job_id=queued["job_id"])
            before = core.store.rows("SELECT * FROM model_usage")
            admissions = core.store.rows("SELECT * FROM model_admissions")
            with pytest.raises(CoreError, match="REM_ALREADY_ATTEMPTED"):
                budget.reserve(core.store, core.settings, job["job_id"], "rem",
                    [{"role": "user", "content": "should never regenerate this draft"}],
                    core.clock(), clock=core.clock)
            assert core.store.rows("SELECT * FROM model_usage") == before
            assert core.store.rows("SELECT * FROM model_admissions") == admissions
            assert await rem.run(core, job)
            changes = core.store.db.total_changes
            assert rem.records(core)[0]["text"] == retained["text"]
            assert core.store.db.total_changes == changes
            core.store.execute("UPDATE rem_records SET output_json="
                               "json_set(output_json,'$.text','tampered')")
            changes = core.store.db.total_changes
            assert not rem.records(core)[0]["source_available"]
            assert not rem.records(core)[0]["text"]
            assert core.store.db.total_changes == changes
    run(scenario())

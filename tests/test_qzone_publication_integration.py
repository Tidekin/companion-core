"""Signed capture, isolated space reader/Life worker, owner approval and independent Bridge."""

import asyncio
import json
import sys
from dataclasses import asdict, replace
from datetime import timedelta

import pytest

from companion import owner_authority
from companion.life_protocol import LifeLoopConfig, LifeQZoneSourceGrant, LifeShadowApproval
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.qzone import review_source
from companion.social_autonomy_protocol import SocialAutonomyConfig
from tests.conftest import run
from tests.support.native_consumer_entry import environment
from tests.support.owner_authority import activate
from tests.test_life_worker_integration import process_worker
from tests.test_qzone_integration import options, reader_process, selected


async def retained_space_draft(core, entry, tmp_path):
    event, control, account, grant, _, _ = await selected(core, entry, tmp_path)
    await reader_process(core, entry.client, account)
    source = core.store.one("SELECT * FROM observations")
    fingerprint = review_source(core.store, core.settings, source["event_id"], core.clock())[
        "source_sha256"]
    original = owner_authority.current(core.store, core.settings, core.clock)
    stopped = await entry.client.post("/internal/v1/owner-authority/stop", headers=control,
        json={"authority": original.authority.as_dict(), "operation_id": "publication-read-done"})
    assert stopped.status_code == 200, stopped.text
    entry.runtime.stop()
    core.settings.life_loop = LifeLoopConfig(mode="SHADOW")
    core.settings.wake_enabled = True
    conversation = core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
                                 (event["event_id"],))["conversation_id"]
    scope = LifeShadowApproval(scope="owner_private_life_shadow_v1", authorization_sha256="b" * 64,
        conversation_id=conversation, source_event_ids=[source["event_id"]], purposes=[],
        qzone_draft_sources=[LifeQZoneSourceGrant(
            grant_id="synthetic-publication-source", revision=1, purpose="qzone_draft",
            event_id=source["event_id"], source_sha256=fingerprint,
            expires_at=core.clock.now + timedelta(hours=1), audience="owner_only",
            owner_qq_id=core.settings.owner_qq_id)], max_items=1,
        expires_at=core.clock.now + timedelta(hours=1))
    policy = OwnerGrantPolicy.model_validate({**original.policy.model_dump(),
        "purposes": [*original.policy.purposes, "life"], "life_shadow": scope})
    ref = activate(core, policy, revision=stopped.json()["authority"]["revision"],
                   operation="publication-draft-scope")[0]["authority"]
    core.clock.advance(1)
    outcome = await process_worker(core, tmp_path, "qzone", source["event_id"])
    assert outcome["provider_calls"] == 1
    result = core.store.one("SELECT * FROM life_activity_results")
    draft = core.life_topics.retained_result(result["job_id"])["qzone_draft"]
    return control, ref, grant, draft


@pytest.mark.parametrize("outcome", ["ACK", "UNKNOWN", "REVOKE", "FORGET"])
def test_public_approval_through_real_http_and_independent_bridge(core, tmp_path, outcome):
    async def scenario():
        cfg = SocialAutonomyConfig(enabled=True, scope="qq_social_v1",
            authorization_sha256="a" * 64, qzone_enabled=True, qzone_public_enabled=True,
            max_output_tokens=512)
        async with environment(core, tmp_path, purposes=("chat", "content", "social",
            "account_actions", "qzone_publication"), core_options={**options(),
            "social_autonomy": cfg}) as (entry, sends):
            control, ref, grant, draft = await retained_space_draft(core, entry, tmp_path)
            prefix = "/internal/v2/qzone/publications"
            headers = {**control, "Idempotency-Key": "http-prepare-public-space"}
            prepared = await entry.client.post(prefix, headers=headers, json={
                "job_id": draft["draft_id"], "expected_content_sha256": draft["content_sha256"]})
            assert prepared.status_code == 201, prepared.text
            review = prepared.json()
            assert review["state"] == "WAITING_APPROVAL" and review["sources"][0]["text"]
            assert not sends and not core.store.rows("SELECT * FROM outbox")
            assert "PRIVATE_OWNER_SENTINEL" not in prepared.text
            route = prefix + "/" + review["plan_id"] + "/decision"
            decision = {"expected_payload_sha256": review["payload_sha256"], "decision": "APPROVE",
                        "source_sharing_confirmed": True}
            headers["Idempotency-Key"] = "http-approve-public-space"
            approved = await entry.client.post(route, headers=headers, json=decision)
            assert approved.status_code == 200, approved.text
            assert approved.json()["state"] == "PENDING"
            assert (await entry.client.post(route, headers=headers, json=decision)).json() == (
                approved.json())
            assert len(core.store.rows("SELECT * FROM outbox")) == 1
            bridge = replace(entry.config, owner_authority=ref,
                             data_dir=str(tmp_path / "public-account-bridge"))
            path = tmp_path / "public-account-host.json"
            path.write_text(json.dumps({"config": asdict(bridge), "now": core.clock(),
                "expected_body": {"content": review["text"], "ugc_right": 1},
                "outcome": outcome}), encoding="utf-8")
            child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
                "tests.native_account_host_process", str(path), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                async with asyncio.timeout(45):
                    signal = await child.stdout.readline()
                    while signal and signal.strip() != b"BEFORE_ACCOUNT_CHECK":
                        assert "stage" in json.loads(signal), signal
                        signal = await child.stdout.readline()
                    assert signal.strip() == b"BEFORE_ACCOUNT_CHECK", signal
                    if outcome == "REVOKE":
                        revoked = await entry.client.post(route, headers={**control,
                            "Idempotency-Key": "http-revoke-public-space"}, json={**decision,
                            "decision": "REVOKE", "source_sharing_confirmed": False})
                        assert revoked.status_code == 200, revoked.text
                    if outcome == "FORGET":
                        erased = await entry.client.post("/internal/v2/source-grants/"
                            + grant["grant_id"] + "/forget", headers=control)
                        assert erased.status_code == 200, erased.text
                    output, error = await child.communicate(b"continue\n")
                    assert child.returncode == 0, error.decode()
                    sent = outcome in {"ACK", "UNKNOWN"}
                    assert json.loads(output.splitlines()[-1]) == {
                        "sent": int(sent), "attempt": outcome if sent else "NOT_SENT"}
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            state = core.store.one("SELECT state FROM outbox")["state"]
            assert state == {"ACK": "SENT", "UNKNOWN": "DELIVERY_UNCERTAIN",
                             "REVOKE": "CANCELLED", "FORGET": "CANCELLED"}[outcome]
            shown = await entry.client.get(prefix + "/" + review["plan_id"], headers=control)
            if outcome == "ACK":
                assert shown.json()["receipt"] == {"result": "SUCCEEDED",
                    "resource_id": "independent-synthetic-account"}
            if outcome == "UNKNOWN":
                # A repeated approval cannot retry an ambiguous external effect.
                again = await entry.client.post(route, headers=headers, json=decision)
                assert again.json()["state"] == "DELIVERY_UNCERTAIN"
                assert core.store.one("SELECT attempt_count FROM outbox")["attempt_count"] == 1
            assert not sends and not core.store.rows("SELECT * FROM memories")
            assert len(core.store.rows("SELECT * FROM model_usage")) == 1

    run(scenario())

"""Node boundary tests and real TLS/WS/Core entrypoint with independent local processes."""

import json
import time

import httpx
import pytest

from companion.store import encode
from companion.voice_config import VoiceASRPolicy
from companion.voice_protocol import (
    MAX_RESULT_BODY_BYTES,
    ROUTE_PREFIX,
    VoiceClaimResponse,
    VoiceLeaseRef,
    canonical_bytes,
)
from tests.conftest import run
from tests.support.voice_jobs import TOKEN, create_tts, prepared, result
from tests.support.voice_node_api import app_for, claim_body, node_wall_clock
from tests.support.voice_node_worker import node as node  # noqa: F401 -- pytest fixture


def test_api_auth_idempotency_path_and_result_receipt(core, monkeypatch):
    jobs, message_id, _, _ = prepared(core, monkeypatch)
    job = create_tts(core, jobs, message_id)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(core)), base_url="http://test"
        ) as client:
            body = claim_body(core)
            headers = {
                "Authorization": "Bearer " + TOKEN,
                "Idempotency-Key": f"voice-claim:{body['incarnation']}:{body['sequence']}",
            }
            for token in (
                "wrong-credential-" + "x" * 32,
                core.settings.bridge_token.get_secret_value(),
            ):
                response = await client.post(
                    ROUTE_PREFIX + "/claim",
                    json=body,
                    headers=headers | {"Authorization": "Bearer " + token},
                )
                assert response.status_code == 401
            assert core.store.rows("SELECT * FROM voice_node_state") == []
            assert (
                await client.post(
                    ROUTE_PREFIX + "/claim", json=body, headers={"Authorization": "Bearer " + TOKEN}
                )
            ).status_code == 400
            claimed = await client.post(ROUTE_PREFIX + "/claim", json=body, headers=headers)
            assert claimed.status_code == 200 and claimed.headers["cache-control"] == "no-store"
            # Normalized DTO hashing makes key replay independent of wire whitespace/key order.
            replayed = await client.post(
                ROUTE_PREFIX + "/claim",
                content=json.dumps(dict(reversed(list(body.items()))), indent=2),
                headers=headers | {"Content-Type": "application/json"},
            )
            assert replayed.json() == claimed.json()
            ref = VoiceLeaseRef.from_lease(VoiceClaimResponse.model_validate(claimed.json()).lease)
            path = ROUTE_PREFIX + "/jobs/" + job["job_id"]
            bad = await client.post(
                ROUTE_PREFIX + "/jobs/voice_" + "f" * 32 + "/input",
                json=ref.model_dump(),
                headers=headers,
            )
            assert bad.status_code == 409
            assert jobs.status(job["job_id"])["state"] == "LEASED"
            source = await client.post(path + "/input", json=ref.model_dump(), headers=headers)
            assert source.status_code == 200 and source.headers["cache-control"] == "no-store"
            value = result(ref)
            ready = await client.post(
                path + "/control",
                json={
                    "ref": ref.model_dump(),
                    "phase": "RESULT_READY",
                    "resource_state": "READY",
                    "result_sha256": value.result_sha256,
                },
                headers=headers,
            )
            assert ready.json()["action"] == "CONTINUE"
            headers["Idempotency-Key"] = f"voice-result:{ref.job_id}:{ref.generation}"
            accepted = await client.put(
                path + "/result",
                content=canonical_bytes(value),
                headers=headers | {"Content-Type": "application/json"},
            )
            assert accepted.status_code == 200 and accepted.json()["status"] == "COMMITTED"
            again = await client.put(
                path + "/result", json=value.model_dump(mode="json"), headers=headers
            )
            assert again.json() == accepted.json()
            assert jobs.result(job["job_id"]) == value.payload

    run(scenario())


@pytest.mark.parametrize("reserved", ["bridge_token", "control_token", "diary_admin_token"])
def test_voice_credential_cannot_alias_owner_bridge_or_admin(core, monkeypatch, reserved):
    prepared(core, monkeypatch)
    from pydantic import SecretStr

    setattr(core.settings, reserved, SecretStr(TOKEN))

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(core)), base_url="http://test"
        ) as client:
            body = claim_body(core)
            response = await client.post(
                ROUTE_PREFIX + "/claim",
                json=body,
                headers={
                    "Authorization": "Bearer " + TOKEN,
                    "Idempotency-Key": f"voice-claim:{body['incarnation']}:{body['sequence']}",
                },
            )
            assert response.status_code == 401
            assert core.store.rows("SELECT * FROM voice_node_state") == []

    run(scenario())


def test_boundary_auth_precedes_body_read_and_stream_limit_is_enforced(core, monkeypatch):
    prepared(core, monkeypatch)

    async def scenario():
        reads = []

        async def oversized():
            reads.append(True)
            yield b"x" * (MAX_RESULT_BODY_BYTES + 1)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(core, boundary_only=True)),
            base_url="http://test",
        ) as client:
            path = ROUTE_PREFIX + "/jobs/voice_" + "a" * 32 + "/result"
            denied = await client.put(
                path, content=oversized(), headers={"Content-Type": "application/json"}
            )
            assert denied.status_code == 401 and not reads
            response = await client.put(
                path,
                content=oversized(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN},
            )
            assert response.status_code == 413 and len(reads) == 1
            duplicate = await client.post(
                ROUTE_PREFIX + "/claim",
                content=b'{"node_id":1,"node_id":2}',
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN},
            )
            assert duplicate.status_code == 400 and "node_id" not in duplicate.text

    run(scenario())


@pytest.mark.parametrize("change", ["cancelled", "revision", "source", "scope", "fenced_epoch"])
def test_disabled_asr_input_state_preserves_source_authority(core, monkeypatch, change):
    _, _, event_id, _ = prepared(core, monkeypatch, make_reply=False,
                                asr=VoiceASRPolicy(enabled=False))

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(core)), base_url="http://test",
            headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
        ) as client:
            path = f"/internal/v1/voice-inputs/{event_id}/state"
            initial = await client.get(path)
            assert initial.status_code == 200, initial.text
            assert initial.json()["state"] == "FALLBACK"
            assert not initial.json()["records"][0]["upload_allowed"]
            if change == "cancelled":
                core.store.execute("UPDATE jobs SET state='CANCELLED' WHERE event_id=?",
                                   (event_id,))
            elif change == "revision":
                core.store.execute("UPDATE conversations SET reply_revision=reply_revision+1")
            elif change == "source":
                event = core.store.one("SELECT body_json FROM events WHERE event_id=?", (event_id,))
                body = json.loads(event["body_json"])
                body["text"] = "source changed after capture"
                core.store.execute("UPDATE events SET body_json=? WHERE event_id=?",
                                   (encode(body), event_id))
            elif change == "scope":
                core.settings.voice = core.settings.voice.model_copy(
                    update={"job_ttl_seconds": 91})
            else:
                event = core.store.one("SELECT * FROM events WHERE event_id=?", (event_id,))
                value = core.voice._input_authority(event) | {"state": "FENCED"}
                core.store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)",
                                   ("voice_input:" + event_id, encode(value)))
                core.store.execute(
                    "INSERT OR REPLACE INTO runtime_meta VALUES ('privacy_epoch','1')")
            before = core.store.db.total_changes
            denied = await client.get(path)
            assert denied.status_code in {403, 409}, denied.text
            assert core.store.db.total_changes == before
            assert not core.store.rows("SELECT * FROM voice_jobs")
            assert not core.store.rows("SELECT * FROM model_usage")

    run(scenario())


@pytest.mark.parametrize("advancing", [False, True])
def test_node_wall_clock_preserves_parent_anchor(monkeypatch, advancing):
    clock = node_wall_clock(1000.0, 200.0 if advancing else None)
    # The first reading occurs after child startup; the anchor must not reset there.
    monkeypatch.setattr(time, "monotonic", lambda: 208.0)
    assert clock() == (1008.0 if advancing else 1000.0)
    monkeypatch.setattr(time, "monotonic", lambda: 210.0)
    assert clock() == (1010.0 if advancing else 1000.0)

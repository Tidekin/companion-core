"""A retry of an accepted input exposes the original job's current state."""

import asyncio
from pathlib import Path

import httpx
import jsonschema
import yaml

from tests.conftest import event
from tests.support.core_http import real_core_http


def test_published_ingest_reply_accepts_live_replay_states(core):
    document = yaml.safe_load((Path(__file__).parents[1] / "contracts/openapi.v1.yaml")
                              .read_text(encoding="utf-8"))
    schema = document["components"]["schemas"]["IngestReply"]
    body = event(core).model_dump(mode="json")
    headers = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value(),
               "X-Idempotency-Key": "contract-replay"}

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
        ) as client:
            async def post():
                response = await client.post("/internal/v1/inbox/events", json=body,
                                             headers=headers)
                assert response.status_code == 202
                value = response.json()
                jsonschema.validate(value, schema)
                return value

            first = await post()
            assert not first["duplicate"] and first["state"] == "QUEUED"
            job = core.claim_job("contract-worker", job_id=first["job_id"])
            assert (await post()) == {**first, "duplicate": True, "state": "RUNNING"}
            core.fail_job(job, "MODEL_RETRYABLE")
            assert (await post()) == {**first, "duplicate": True, "state": "RETRY_WAIT"}
            for terminal in ("CANCELLED", "SUCCEEDED", "FAILED"):
                core.store.execute("UPDATE jobs SET state=? WHERE job_id=?",
                                   (terminal, job["job_id"]))
                assert (await post()) == {**first, "duplicate": True, "state": terminal}

    asyncio.run(scenario())

"""Discovery and caption use original owner grants, leases and paid responsibilities."""

import asyncio
import json
import sys

import httpx
import pytest

from companion import meme_service, owner_authority
from companion.contracts import CoreError
from companion.model import ModelClient
from companion.owner_authority_protocol import AuthorityStop
from tests.conftest import run
from tests.meme_authority_fixture import environment
from tests.support.evaluation_process import process_config
from tests.support.meme_core import CAPTION, body
from tests.support.meme_owner_authority import configure, discovery
from tests.support.meme_service import result, submit


def test_discovery_only_requires_its_original_native_grant(core, tmp_path, monkeypatch):
    paid = configure(core)

    async def scenario():
        async with environment(core, tmp_path, monkeypatch, paid=paid) as values:
            client, config, grant, now = values
            assert core.settings.paid_model.meme_input is None
            event, item = await discovery(core, *values)
            original = core.store.one("SELECT authority_json FROM jobs WHERE job_id=?",
                                       (item["job_id"],))["authority_json"]
            assert json.loads(original) == grant.authority
            owner_authority.stop(core.store, core.settings, core.clock,
                AuthorityStop(operation_id="stop-discovery", authority=grant.authority))
            with pytest.raises(CoreError, match="OWNER_AUTHORITY_DISABLED"):
                meme_service.check(core.store, core.settings, core.clock, item["job_id"],
                    meme_service.DiscoveryCheck(lease_token=item["lease_token"],
                                                attempt_key=item["attempt_key"]))
            assert result(core, item, "UNKNOWN")["state"] == "UNKNOWN"
            assert core.store.one("SELECT authority_json FROM jobs WHERE job_id=?",
                                  (item["job_id"],))["authority_json"] == original
            assert core.store.rows("SELECT * FROM model_usage") == []
            with pytest.raises(CoreError, match="OWNER_AUTHORITY_DISABLED"):
                submit(core, event)

    run(scenario())


def test_caption_real_http_and_process_reuse_original_usage(core, tmp_path, monkeypatch):
    paid = configure(core, caption=True)

    async def scenario():
        async with environment(core, tmp_path, monkeypatch, paid=paid) as values:
            client, config, grant, now = values
            event, item = await discovery(core, *values)
            result(core, item)
            request = body(core, event, item, now)
            response = await client.post("/internal/v1/memes/captions", json=request)
            assert response.status_code == 200, response.text
            job_id = response.json()["job_id"]
            source = core.store.one("SELECT authority_json FROM jobs WHERE job_id=?", (job_id,))
            assert json.loads(source["authority_json"]) == grant.authority
            config_path, output = tmp_path / "worker.json", tmp_path / "worker-report.json"
            process_config(core, config_path)
            child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
                "tests.meme_native_worker", str(config_path), core.clock(), str(output),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                _, error = await asyncio.wait_for(child.communicate(), 45)
                assert child.returncode == 0, error.decode()
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            report = json.loads(output.read_text("utf-8"))
            assert report["progressed"] and len(report["calls"]) == 1
            outcome = (await client.post("/internal/v1/memes/captions/" + job_id, json={})).json()
            assert outcome["state"] == "SUCCEEDED", outcome
            assert outcome["usage"]["state"] == "SETTLED"
            repeated = await client.post("/internal/v1/memes/captions", json=request)
            assert repeated.json() == outcome
            assert len(core.store.rows("SELECT * FROM model_usage")) == 1
            assert core.store.rows("SELECT * FROM outbox") == []

    run(scenario())


@pytest.mark.parametrize("fence", ["stop", "source", "unknown"])
def test_native_caption_final_boundary_preserves_dispatch_knowledge(
    core, tmp_path, monkeypatch, fence,
):
    paid = configure(core, caption=True)

    async def scenario():
        async with environment(core, tmp_path, monkeypatch, paid=paid) as values:
            client, config, grant, now = values
            event, item = await discovery(core, *values)
            result(core, item)
            response = await client.post("/internal/v1/memes/captions",
                                         json=body(core, event, item, now))
            assert response.status_code == 200, response.text
            job_id = response.json()["job_id"]
            calls = []

            async def hook(request):
                if fence == "stop":
                    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                        operation_id="stop-caption-boundary", authority=grant.authority))
                elif fence == "source":
                    with core.store.transaction():
                        core.ledger.expire_events(core.store, [event["event_id"]], core.clock())

            def provider(request):
                calls.append(request)
                if fence == "unknown":
                    raise httpx.ReadTimeout("synthetic unknown provider result")
                return httpx.Response(200, json={"choices": [{"message": {
                    "content": json.dumps(CAPTION)}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

            async with httpx.AsyncClient(transport=httpx.MockTransport(provider),
                                         event_hooks={"request": [hook]}) as transport:
                core.model = ModelClient(core.settings, transport)
                assert await core.run_once("caption-boundary")
            usage = core.store.one("SELECT state,billed_micro_usd FROM model_usage WHERE job_id=?",
                                   (job_id,))
            assert len(calls) == (1 if fence == "unknown" else 0)
            if fence == "unknown":
                assert usage["state"] == "UNKNOWN" and usage["billed_micro_usd"] > 0
            else:
                assert usage == {"state": "SETTLED", "billed_micro_usd": 0}
            assert core.store.rows("SELECT * FROM outbox") == []

    run(scenario())

"""Native image checks carry strict current authority evidence without another RPC."""

import json
from types import SimpleNamespace

import httpx
import pytest

from companion_onebot_bridge import images
from companion_onebot_bridge.network import private_http_client
from tests.conftest import run
from tests.support.native_image_pipeline import admit_image, claim, pixels
from tests.support.prepare_owner_input import native_spool, window


def test_native_image_check_contains_the_same_complete_authority_receipt(core, tmp_path):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            event, image = await admit_image(core, client, config, plan, tmp_path)
            lease = await claim(client, event, image)
            authority = await client.post("/internal/v1/owner-authority/check", json={
                "authority": plan.authority, "purpose": "image"})
            response = await client.post(
                "/internal/v2/content/" + event["event_id"] + "/image/check",
                json={"lease_token": lease["lease_token"], "source_sha256": image["source_sha256"]})
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert response.json() == {"approved": True, "lease_until": lease["lease_until"],
                                       "authority_check": authority.json()}
            spool, runtime = native_spool(core, tmp_path, config, plan)
            try:
                legacy = SimpleNamespace(config=runtime.config, authority=runtime.authority,
                    clock=runtime.clock, legacy={"budget_journal_id": "budget_" + "f" * 32})
                with pytest.raises(ValueError, match="OWNER_LEGACY_BUDGET_CUSTODY_CHANGED"):
                    images._image_check_receipt(legacy, response.json(), lease["lease_until"])
            finally:
                spool.close()
    run(scenario())


@pytest.mark.parametrize("mutation", [
    "missing_authority", "missing_capture", "capture_key", "identity", "purpose",
    "authority_revision", "missing_capability", "missing_budget", "lease", "approved", "local_stop",
])
def test_native_image_check_rejects_incomplete_or_changed_receipts_before_get(
    core, tmp_path, mutation,
):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            event, image = await admit_image(core, client, config, plan, tmp_path)
            spool, runtime = native_spool(core, tmp_path, config, plan)
            downloads = []

            async def resolve(*_):
                return ["1.1.1.1"]

            async def download(request):
                downloads.append(request)
                return httpx.Response(200, content=pixels(), headers={"Content-Type": "image/png"})

            async def changed(response):
                if not response.request.url.path.endswith("/image/check"):
                    return
                await response.aread()
                value = response.json()
                if mutation == "missing_authority":
                    del value["authority_check"]
                elif mutation == "missing_capture":
                    del value["authority_check"]["capture"]
                elif mutation == "capture_key":
                    value["authority_check"]["capture"]["public_key"] = "f" * 64
                elif mutation == "identity":
                    value["authority_check"]["capture"]["identity_sha256"] = "f" * 64
                elif mutation == "purpose":
                    value["authority_check"]["purpose"] = "chat"
                elif mutation == "authority_revision":
                    value["authority_check"]["authority"]["revision"] += 2
                elif mutation == "missing_capability":
                    del value["authority_check"]["capability_policies"]["image"]
                elif mutation == "missing_budget":
                    del value["authority_check"]["budget_journal_id"]
                elif mutation == "lease":
                    value["lease_until"] = "2099-01-01T00:00:00+00:00"
                elif mutation == "approved":
                    value["approved"] = 1
                else:
                    runtime.stop()
                response._content = json.dumps(value).encode()

            try:
                async with private_http_client(config.core_url, config.bridge_token) as transfer:
                    transfer.event_hooks["response"].append(changed)
                    with pytest.raises(ValueError):
                        await images.upload_original(runtime, transfer, event["event_id"],
                            images.OriginalImage(**image), resolver=resolve,
                            transport=httpx.MockTransport(download))
                assert not downloads
                assert not core.store.rows("SELECT * FROM captured_images")
                assert not core.store.rows("SELECT * FROM model_usage")
            finally:
                spool.close()
    run(scenario())

"""Actual signed API lease reads; provider transport is synthetic and original fees retained."""

import base64
import json

import httpx

from companion.core import Core
from companion_meme_protocol import digest
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, worker_for
from tests.support.core_http import real_core_http
from tests.support.meme_asset_migration import original_work
from tests.support.meme_core_runtime import runtime_case as runtime_case


async def settled_reply(c):
    # Establish this endpoint test's frozen native offer explicitly. The distinct
    # process tests exercise automatic Worker offer generation after integration.
    await c.service.prepare_offer(c.job)
    c.core.store.execute(
        "UPDATE jobs SET state='QUEUED',lease_token=NULL WHERE job_id=?", (c.job["job_id"],)
    )
    provider = Provider(answer("给你温暖的抱抱。⬡" + c.item["asset_sha256"] + "⬡"))
    async with worker_for(c.core, provider) as independent:
        assert await independent.run_once("meme-asset-api-fixture")
    assert len(provider.requests) == 1
    row = c.core.store.one("SELECT * FROM outbox")
    assert (
        row and json.loads(row["content_json"])["image"]["candidate_id"] == c.item["asset_sha256"]
    )


async def lease(c, client):
    response = await client.post(
        "/internal/v1/deliveries/lease",
        json={"worker_id": "meme-core-asset-api", "authority": c.grant.authority, "limit": 1},
    )
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    body = {
        "authority": c.grant.authority,
        "event_id": item["origin_event_id"],
        "reply_revision": item["reply_revision"],
        "lease_token": item["lease_token"],
        "content_sha256": digest(item["content"]),
    }
    return item, body


def test_delivery_byte_read_is_bounded_idempotent_and_lease_scoped(runtime_case):
    c = runtime_case

    async def scenario():
        await settled_reply(c)
        with Core(c.core.settings, clock=c.core.clock) as api_core:
            async with (
                real_core_http(api_core) as port,
                httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}",
                    trust_env=False,
                    headers={"Authorization": "Bearer " + c.config.bridge_token},
                ) as client,
            ):
                item, body = await lease(c, client)
                path = "/internal/v1/memes/deliveries/" + item["message_id"] + "/asset"
                before = original_work(c.core.store)
                response = await client.post(path, json=body)
                assert response.status_code == 200, response.text
                assert response.headers["Cache-Control"] == "no-store"
                value = response.json()
                assert len(response.content) < 6_000_000
                assert base64.b64decode(value["data_base64"]) == c.data
                assert value["asset_id"] == c.asset_id
                assert value["asset_sha256"] == c.item["asset_sha256"]
                assert (
                    value["binding_sha256"]
                    == json.loads(
                        c.core.store.one(
                            "SELECT value FROM runtime_meta WHERE key=?",
                            ("owner_delivery:" + item["message_id"],),
                        )["value"]
                    )["meme_binding_sha256"]
                )
                assert (await client.post(path, json=body)).json() == value
                assert original_work(c.core.store) == before
                invalid = [
                    {**body, "lease_token": "lease_" + "b" * 32},
                    {**body, "content_sha256": "f" * 64},
                    {**body, "event_id": "evt_" + "f" * 64},
                    {**body, "reply_revision": body["reply_revision"] - 1},
                    {**body, "reply_revision": True},
                    {**body, "asset_id": c.asset_id},
                    {**body, "file_path": "private.png"},
                    {**body, "source_url": "https://public.example/image.png"},
                    {**body, "authority": {}},
                ]
                for changed in invalid:
                    denied = await client.post(path, json=changed)
                    assert denied.status_code in {400, 403, 409, 422}, denied.text
                    assert "data_base64" not in denied.json()
                foreign = await client.post(
                    path.replace(item["message_id"], "msg_" + "f" * 32), json=body
                )
                assert foreign.status_code == 403
                assert (
                    await client.post(path, json=body, headers={"Authorization": "Bearer wrong"})
                ).status_code == 401
                assert original_work(c.core.store) == before
                begun = await client.post(
                    "/internal/v1/deliveries/" + item["message_id"] + "/begin-send",
                    json={"lease_token": item["lease_token"]},
                )
                assert begun.status_code == 200, begun.text
                assert (await client.post(path, json=body)).status_code == 200

    run(scenario())


def test_root_erasure_refuses_asset_under_old_lease_and_preserves_fees(runtime_case):
    c = runtime_case

    async def scenario():
        await settled_reply(c)
        with Core(c.core.settings, clock=c.core.clock) as api_core:
            async with (
                real_core_http(api_core) as port,
                httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}",
                    trust_env=False,
                    headers={"Authorization": "Bearer " + c.config.bridge_token},
                ) as client,
            ):
                item, body = await lease(c, client)
                path = "/internal/v1/memes/deliveries/" + item["message_id"] + "/asset"
                assert (await client.post(path, json=body)).status_code == 200
                usage = c.core.store.rows("SELECT * FROM model_usage")
                with c.core.store.transaction():
                    c.core.ledger.expire_events(
                        c.core.store, [c.source["event_id"]], c.core.clock()
                    )
                denied = await client.post(path, json=body)
                assert denied.status_code in {403, 409}, denied.text
                assert "data_base64" not in denied.json()
                assert c.core.store.rows("SELECT * FROM model_usage") == usage

    run(scenario())


def test_old_frozen_delivery_backup_replays_newest_ledger_before_any_asset_read(
    runtime_case, tmp_path
):
    from contextlib import closing

    from cryptography.fernet import Fernet

    from companion.backup import backup, restore
    from companion.contracts import CoreError
    from companion.meme_delivery import AssetRequest, read_asset
    from companion.store import Store

    c = runtime_case
    archive, restored = tmp_path / "frozen.enc", tmp_path / "restored.db"
    key = Fernet.generate_key()

    async def scenario():
        await settled_reply(c)
        with Core(c.core.settings, clock=c.core.clock) as api_core:
            async with (
                real_core_http(api_core) as port,
                httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}",
                    trust_env=False,
                    headers={"Authorization": "Bearer " + c.config.bridge_token},
                ) as client,
            ):
                item, body = await lease(c, client)
                path = "/internal/v1/memes/deliveries/" + item["message_id"] + "/asset"
                assert (await client.post(path, json=body)).status_code == 200
                before = c.core.store.rows("SELECT * FROM model_usage")
                backup(c.core.settings.database, archive, key)
                with c.core.store.transaction():
                    c.core.ledger.expire_events(
                        c.core.store, [c.source["event_id"]], c.core.clock()
                    )
                restore(archive, restored, key, c.core.settings.erasure_ledger)
                with closing(Store(str(restored))) as store:
                    asset = store.one("SELECT * FROM meme_assets")
                    assert (
                        asset["state"] == "ERASED"
                        and asset["data"] is asset["metadata_json"] is None
                    )
                    assert not store.one(
                        "SELECT 1 FROM runtime_meta WHERE key=?",
                        ("meme_offer:" + c.job["event_id"],),
                    )
                    assert (
                        store.one(
                            "SELECT state FROM outbox WHERE message_id=?", (item["message_id"],)
                        )["state"]
                        == "CANCELLED"
                    )
                    assert store.rows("SELECT * FROM model_usage") == before
                    settings = c.core.settings.model_copy(update={"database": str(restored)})
                    import pytest

                    with pytest.raises(CoreError):
                        read_asset(
                            store,
                            settings,
                            c.core.clock,
                            item["message_id"],
                            AssetRequest.model_validate(body),
                        )

    run(scenario())

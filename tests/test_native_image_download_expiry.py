"""Expired native download claims close without changing paid or original-byte custody."""

import json

import pytest

from companion import agent_images, image_spool, owner_authority
from companion.owner_authority_protocol import AuthorityStop
from companion.store import encode
from tests.conftest import run
from tests.support.native_image_pipeline import admit_image, claim, pixels, upload_headers
from tests.support.prepare_owner_input import window


@pytest.mark.parametrize("state", ["FETCHING", "UNKNOWN"])
@pytest.mark.parametrize("stopped", [False, True])
def test_expired_native_download_closes_without_new_authority_or_refund(
    core, tmp_path, state, stopped,
):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            event, image = await admit_image(core, client, config, plan, tmp_path)
            lease = await claim(client, event, image)
            if state == "UNKNOWN":
                core.store.execute("UPDATE content_items SET state='UNKNOWN',"
                                   "error_code='CONTENT_PROCESSOR_LOST'")
            before = core.store.one("SELECT * FROM content_items")
            agent_images.expire_downloads(core.content)
            assert core.store.one("SELECT * FROM content_items") == before
            core.clock.advance(61)
            if stopped:
                owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                    operation_id="stop-before-image-expiry", authority=plan.authority))
            usage = core.store.rows("SELECT * FROM model_usage")
            await core.content.run_once()
            after = core.store.one("SELECT * FROM content_items")
            assert after["state"] == "FAILED"
            assert after["error_code"] == "CONTENT_IMAGE_DOWNLOAD_EXPIRED"
            assert {key: value for key, value in after.items()
                    if key not in {"state", "error_code", "updated_at"}} == {
                key: value for key, value in before.items()
                if key not in {"state", "error_code", "updated_at"}}
            assert after["lease_token"] == lease["lease_token"]
            assert after["reserved_bytes"] > 0
            await core.content.run_once()
            assert core.store.one("SELECT * FROM content_items") == after
            assert core.store.rows("SELECT * FROM model_usage") == usage
            assert not core.store.rows("SELECT * FROM captured_images")
            assert not core.store.rows("SELECT * FROM model_admissions")
            if not stopped:
                result = await client.post("/internal/v2/content/" + event["event_id"]
                    + "/image/claim", json={"request_key": "image:0",
                                           "source_sha256": image["source_sha256"]})
                assert result.status_code == 409
                assert result.json()["error"] == "CONTENT_IMAGE_ALREADY_FAILED"
    run(scenario())


@pytest.mark.parametrize("evidence", [
    "usage", "captured", "spool", "source_hash", "capture_signature", "job_authority",
    "source_pointer", "legacy", "staged", "other_unknown", "running",
])
def test_download_expiry_does_not_reinterpret_unproven_or_staged_responsibility(
    core, tmp_path, evidence,
):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            event, image = await admit_image(core, client, config, plan, tmp_path)
            lease = await claim(client, event, image)
            item = core.store.one("SELECT * FROM content_items")
            source_key = "owner_input:" + event["event_id"]
            if evidence == "usage":
                core.store.execute("UPDATE content_items SET usage_request_id='retained-request'")
            elif evidence == "captured":
                response = await client.post("/internal/v2/content/" + event["event_id"] + "/image",
                    content=pixels(), headers=upload_headers(plan, event, image, lease, pixels()))
                assert response.status_code == 200, response.text
                core.store.execute("UPDATE content_items SET state='FETCHING',"
                                   "media_sha256='',fingerprint=''")
            elif evidence == "spool":
                path = image_spool._path(core.store, item["item_id"])
                path.parent.mkdir(mode=0o700, exist_ok=True)
                path.write_bytes(b"interrupted original write")
            elif evidence == "source_hash":
                body = json.loads(core.store.one('SELECT body_json FROM events WHERE event_id=%s',
                                                (event["event_id"],))["body_json"])
                body["rich"]["segments"][0]["source_sha256"] = "f" * 64
                core.store.execute('UPDATE events SET body_json=%s WHERE event_id=%s',
                                   (encode(body), event["event_id"]))
            elif evidence == "capture_signature":
                saved = json.loads(core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                                                 (source_key,))["value"])
                saved["capture"]["signature"] = "0" * 128
                core.store.execute('UPDATE runtime_meta SET value=%s WHERE key=%s',
                                   (encode(saved), source_key))
            elif evidence == "job_authority":
                core.store.execute("UPDATE jobs SET authority_json='{}' WHERE event_id=%s",
                                   (event["event_id"],))
            elif evidence == "source_pointer":
                core.store.execute(
                    'UPDATE content_items SET '
                    "target_url='https://example.invalid/x'"
                )
            elif evidence == "legacy":
                core.store.execute('DELETE FROM runtime_meta WHERE key=%s', (source_key,))
            elif evidence == "staged":
                core.store.insert("runtime_meta", key="live_text_image_upload:" + item["item_id"],
                                  value="{}")
            elif evidence == "other_unknown":
                core.store.execute("UPDATE content_items SET state='UNKNOWN',"
                                   "error_code='CONTENT_VISION_UNCERTAIN'")
            else:
                core.store.execute("UPDATE content_items SET state='RUNNING'")
            core.clock.advance(61)
            before = core.store.one("SELECT * FROM content_items")
            agent_images.expire_downloads(core.content)
            assert core.store.one("SELECT * FROM content_items") == before
    run(scenario())

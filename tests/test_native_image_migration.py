"""The 0041 copy preserves verified v1 evidence without issuing native authority."""

import hashlib
import json
from contextlib import ExitStack, closing
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import agent_images, image_spool, qq_images
from companion.contracts import CoreError
from companion.core import Core
from companion.store import Store, after, encode, migrate, uid
from companion_onebot_bridge.event_normalizer import normalize
from tests.conftest import run
from tests.support.onebot_events import raw_event
from tests.test_live_image_input import options as legacy_image_options
from tests.test_native_image_pipeline import pixels
from tests.test_onebot_capture import pinned_plan
from tests.test_prepare_owner_input import window
from tests.test_test_fixtures import historical_database


@pytest.mark.parametrize("retire", ["expiry", "recall"])
def test_signed_v1_image_copy_preserves_bytes_and_never_upgrades_permission(core, tmp_path, retire):
    settings = core.settings.model_copy(update={"database": str(tmp_path / "fixture-current.db")})
    migrate(settings.database)
    with Core(settings, clock=core.clock) as legacy, ExitStack() as cleanup:
        plan_directory = tmp_path / "legacy-plan"
        plan_directory.mkdir(mode=0o700)
        plan_directory.chmod(0o700)
        config, _ = pinned_plan(legacy, plan_directory, 3001,
                                 live=True, approved=True, rich_input_scope="references_v1",
                                 scope_options=legacy_image_options())
        approval = legacy.settings.live_text_window
        database = historical_database(legacy.store, tmp_path / "legacy-image.db",
                                        "0040_owner_authority")
        now, item_id, job_id = legacy.clock(), uid("content"), uid("job")
        frame = encode(raw_event(time=int(legacy.clock.now.timestamp()), message=[
            {"type": "image", "data": {"url": "https://gchat.qpic.cn/legacy-original"}}]))
        event = normalize(frame, config, now=legacy.clock.now.timestamp(),
                          rich_input_scope="references_v1", image_scope="owner_image_v1")
        image = event["payload"]["rich"]["segments"][0]
        store = cleanup.enter_context(closing(Store(database)))
        conversation = uid("conversation")
        store.insert("conversations", conversation_id=conversation, platform="qq",
            platform_session_key=config.origin, scope="private", owner_actor_id="owner",
            created_at=now)
        store.insert("events", event_id=event["event_id"], schema_version=1, source="qq.onebot",
            source_event_id=event["source_event_id"], conversation_id=conversation,
            actor_id="owner", event_type="message.received", occurred_at=event["occurred_at"],
            ingested_at=now, privacy_scope="private", body_json=encode(event["payload"]),
            trace_id=event["trace_id"])
        store.insert("jobs", job_id=job_id, event_id=event["event_id"], kind="chat",
            state="SUCCEEDED", idempotency_key=job_id, created_at=now, updated_at=now)
        data, token = pixels(), uid("content_lease")
        media_hash = hashlib.sha256(data).hexdigest()
        proof = {"plan_sha256": approval.plan_sha256, "event_id": event["event_id"],
            "source_sha256": image["source_sha256"], "lease_token": token,
            "media_sha256": media_hash, "mime": "image/png"}
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(config.capture_signing_key))
        proof["signature"] = key.sign(qq_images.UPLOAD_DOMAIN + encode(proof).encode()).hex()
        assert qq_images.upload_proof(approval, event["event_id"], image, token,
                                      media_hash, "image/png", proof) == proof
        expiry = after(now, 30)
        store.insert("content_items", item_id=item_id, owner_event_id=event["event_id"],
            kind="qq_image", slot=0, state="SUCCEEDED", captured_privacy_epoch=0,
            media_sha256=media_hash,
            fingerprint=hashlib.sha256(b"image/png\0" + data).hexdigest(),
            lease_token=token, deadline=after(now, 15), created_at=now, updated_at=now)
        row = store.one("SELECT * FROM content_items WHERE item_id=?", (item_id,))
        image_spool.put(store, row, data, "image/png", expiry)
        path = image_spool.directory(store) / (item_id + ".raw")
        original_bytes = path.read_bytes()
        frozen = encode({"images": [{"item_id": item_id, "upload_proof": proof}],
                         "original_expires_at": expiry})
        store.insert("runtime_meta", key="live_text_image:" + job_id, value=frozen)
        journal = Path(legacy.settings.paid_model.journal_path).read_bytes()
        migrate(str(database), "0041_agent_conversations")
        migrate(str(database), "0041_agent_conversations")
        assert store.one("SELECT * FROM captured_images") == {
            "item_id": item_id, "proof_json": encode(proof), "mime": "image/png",
            "expires_at": expiry}
        assert store.one("SELECT value FROM runtime_meta WHERE key=?",
                         ("live_text_image:" + job_id,))["value"] == frozen
        assert path.read_bytes() == original_bytes
        assert Path(legacy.settings.paid_model.journal_path).read_bytes() == journal
        assert not store.one("SELECT 1 FROM owner_authority_revisions")
        assert not store.one("SELECT 1 FROM agent_image_inputs")

        async def native_rejects_original_proof():
            async with window(core, tmp_path / "native", media=True, content=True) as values:
                _, _, native = values
                saved_proof = json.loads(store.one(
                    "SELECT proof_json FROM captured_images")["proof_json"])
                with pytest.raises(CoreError, match="OWNER_IMAGE_UPLOAD_INVALID"):
                    agent_images.upload_proof(core.store, core.settings, native.authority,
                        event["event_id"], image, token, media_hash, "image/png",
                        saved_proof)
                assert not core.store.one("SELECT 1 FROM model_usage")
        run(native_rejects_original_proof())
        if retire == "recall":
            store.execute("UPDATE events SET redacted=1 WHERE event_id=?", (event["event_id"],))
            agent_images.erase_images(store, [event["event_id"]])
        image_spool.cleanup(store, after(expiry, 1) if retire == "expiry" else now,
                            retain_completed=True)
        assert not path.exists()

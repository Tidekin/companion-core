"""An old signed ordinary-image queue survives retry before INPUT_APPROVED."""

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion_onebot_bridge import images, pending_media, voice_runtime
from companion_onebot_bridge.spool import encode
from companion_onebot_bridge.window import digest
from tests.support.onebot_images import image_segment
from tests.test_qq_face_images import face


def queue_window(db):
    key = Ed25519PrivateKey.from_private_bytes(b"\x42" * 32)
    config = SimpleNamespace(capture_signing_key=key.private_bytes_raw().hex(),
                             capture_public_key=key.public_key().public_bytes_raw().hex())
    journal, capacities = [], []

    def append(kind, data):
        journal.append({"kind": kind, "data": data})
        return digest(journal)

    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE test_window(journal_head TEXT)")
    db.execute("INSERT INTO test_window VALUES ('initial')")
    db.execute("CREATE TABLE window_inputs(event_id TEXT,capture_json TEXT,input_hash TEXT)")
    event, capture = {"event_id": "evt_legacy_image"}, {"input_sha256": "b" * 64}
    db.execute("INSERT INTO window_inputs VALUES (?,?,?)",
               (event["event_id"], encode(capture), capture["input_sha256"]))
    db.commit()
    # Historical WindowPlan instances still carry their original scope dictionary;
    # the pre-voice plan omits the newer voice authority fields entirely.
    plan = SimpleNamespace(sha256="a" * 64, config=config,
                           value={"live_text": {"image_scope": "owner_image_v1"}})
    window = SimpleNamespace(db=db, plan=plan,
                             spool=SimpleNamespace(capacity=capacities.append), append=append)
    assert voice_runtime.enabled(window) is False
    return window, event, capture, journal, capacities


@pytest.mark.parametrize("count", [1, 2])
def test_legacy_signed_queue_duplicate_keeps_exact_manifest_and_journal(tmp_path, count):
    with closing(sqlite3.connect(tmp_path / "legacy.db")) as db:
        window, event, capture, journal, capacities = queue_window(db)
        raw = {"message": [image_segment() for _ in range(count)]}
        originals = images.original_images(raw["message"])
        # This is the persisted pre-face schema, independently of today's dataclass fields.
        value = {"window_sha256": window.plan.sha256, "event_id": event["event_id"],
                 "capture_sha256": digest(capture), "input_sha256": capture["input_sha256"],
                 "images": [{"index": image.index, "slot": image.slot,
                             "source_sha256": image.source_sha256, "url": image.url}
                            for image in originals]}
        value["signature"] = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(
            window.plan.config.capture_signing_key)).sign(
                pending_media.DOMAIN + encode(value).encode()).hex()
        pending_media.prepare(window)
        head = window.append("MEDIA_QUEUED", {
            "event_hash": digest(event["event_id"]), "manifest_sha256": digest(value),
            "capture_sha256": digest(capture), "image_count": count,
        })
        with db:
            db.execute("INSERT INTO automatic_media VALUES (?,?,?,?,?,?,0,'PENDING',NULL)",
                       (event["event_id"], window.plan.sha256, digest(capture), digest(value),
                        encode(value), count))
            db.execute("UPDATE test_window SET journal_head=?", (head,))
        before = dict(db.execute("SELECT * FROM automatic_media").fetchone())
        journal_bytes = encode(journal).encode()
        for _ in range(2):
            pending_media.queue(window, event, raw, capture)
            after = dict(db.execute("SELECT * FROM automatic_media").fetchone())
            assert after == before
            assert after["manifest_json"].encode() == encode(value).encode()
            assert after["manifest_sha256"] == digest(value)
            assert encode(journal).encode() == journal_bytes
            assert db.execute("SELECT journal_head FROM test_window").fetchone()[0] == head
        assert len(journal) == 1 and journal[0]["kind"] == "MEDIA_QUEUED"
        assert capacities == []
        assert pending_media.manifest(window, before) == originals
        pending_media.verify_history(window, [(window.plan, journal)])

        changed_raw = {"message": [image_segment("https://gchat.qpic.cn/changed.png")]}
        with pytest.raises(ValueError, match="AUTOMATIC_MEDIA_CHANGED"):
            pending_media.queue(window, event, changed_raw, capture)
        assert dict(db.execute("SELECT * FROM automatic_media").fetchone()) == before
        assert encode(journal).encode() == journal_bytes

        # Omitting null in new serialization does not relax verification of old signatures.
        changed = json.loads(before["manifest_json"])
        changed["images"][0]["face_id"] = None
        forged = {**before, "manifest_json": encode(changed), "manifest_sha256": digest(changed)}
        with pytest.raises(ValueError, match="AUTOMATIC_MEDIA_CHANGED"):
            pending_media.manifest(window, forged)


def test_real_face_field_and_ordinary_image_shape_survive_signed_queue(tmp_path):
    with closing(sqlite3.connect(tmp_path / "mixed.db")) as db:
        window, event, capture, journal, capacities = queue_window(db)
        raw = {"message": [image_segment(), face("277")]}
        pending_media.queue(window, event, raw, capture)
        before = dict(db.execute("SELECT * FROM automatic_media").fetchone())
        value = json.loads(before["manifest_json"])
        assert set(value) == {"window_sha256", "event_id", "capture_sha256", "input_sha256",
                              "images", "signature"}
        assert set(value["images"][0]) == {"index", "slot", "source_sha256", "url"}
        assert value["images"][1]["face_id"] == "277"
        assert set(value["images"][1]) == {"index", "slot", "source_sha256", "url", "face_id"}
        assert pending_media.manifest(window, before) == images.original_images(raw["message"])
        pending_media.queue(window, event, raw, capture)
        assert dict(db.execute("SELECT * FROM automatic_media").fetchone()) == before
        assert len(journal) == len(capacities) == 1
        pending_media.verify_history(window, [(window.plan, journal)])

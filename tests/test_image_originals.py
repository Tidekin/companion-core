"""Originals cross process boundaries through an expiring store, never durable events."""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from copy import deepcopy

import pytest
from cryptography.fernet import Fernet

from companion import image_spool
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from companion.image_input import ImageInputPolicy, messages_and_estimate, text_and_estimate
from companion.privacy import advance_epoch
from companion.store import Store, after, encode, uid
from tests.conftest import ingest
from tests.support.live_image_input import png
from tests.support.paid_image_budget import image_messages


@pytest.mark.parametrize("count", [1, 2, 4])
def test_image_estimates_count_each_original_in_both_stages(count):
    policy = ImageInputPolicy(
        approval_sha256="e" * 64, estimated_tokens_per_image=321,
        max_bytes_per_image=50000, max_pixels_per_image=100,
        max_images_per_request=count,
    )
    messages = image_messages()
    messages[0]["content"].extend(deepcopy(messages[0]["content"][1]) for _ in range(count - 1))
    text, estimate = text_and_estimate(policy, messages)
    assert estimate == count * 321
    references = [{"role": "system", "content": "区分观察、记忆和推测。"},
                  {"role": "user", "content": "已批准的可靠事实：偏好轻便。"}]
    flattened, final_estimate = messages_and_estimate(policy, [*references, *messages])
    assert flattened == [*references, {"role": "user", "content": text}]
    assert final_estimate == estimate
    # Count every authorized original across turns; Core authenticates its source.
    current = {"role": "user", "content": "当前问题"}
    history, historical_estimate = messages_and_estimate(policy, [*messages, current])
    assert history == [{"role": "user", "content": text}, current]
    assert historical_estimate == estimate
    with pytest.raises(CoreError, match="IMAGE_INPUT_INVALID"):
        messages_and_estimate(policy, [*messages, *messages])


def staged_original(core):
    event_id = ingest(core)["event_id"]
    data, item_id = png(), uid("content")
    now = core.clock()
    core.store.insert(
        "content_items", item_id=item_id, owner_event_id=event_id, kind="qq_image", slot=0,
        state="QUEUED", captured_privacy_epoch=0, reserved_bytes=len(data),
        media_sha256=hashlib.sha256(data).hexdigest(),
        fingerprint=hashlib.sha256(b"image/png\0" + data).hexdigest(),
        deadline=after(now, 30), created_at=now, updated_at=now,
    )
    row = core.store.one("SELECT * FROM content_items WHERE item_id=?", (item_id,))
    image_spool.put(core.store, row, data, "image/png", after(now, image_spool.RETENTION_SECONDS))
    return row, data


def test_originals_are_shared_by_new_store_without_entering_db_or_history(core):
    row, data = staged_original(core)
    reopened = Store(core.store.path)
    try:
        assert image_spool.get(reopened, row, core.clock(), 50000) == (data, "image/png")
        assert image_spool.deadline(reopened, row) == after(core.clock(), 300)
        for table in ("events", "runtime_meta", "audit_log"):
            if core.store.one(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
            ):
                assert base64.b64encode(data).decode() not in encode(
                    core.store.rows("SELECT * FROM " + table),
                )
    finally:
        reopened.close()


def test_separate_worker_process_can_read_the_same_bounded_original(core):
    row, data = staged_original(core)
    result = subprocess.run(
        [sys.executable, "-c", "\n".join([
            "import hashlib,json,sys",
            "from companion import image_spool",
            "from companion.store import Store",
            "store=Store(sys.argv[1])",
            "data,mime=image_spool.get(store,json.loads(sys.argv[2]),sys.argv[3],50000)",
            "print(hashlib.sha256(data).hexdigest())",
            "store.close()",
        ]), str(core.store.path), encode(row), core.clock()],
        capture_output=True, text=True, timeout=15,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == hashlib.sha256(data).hexdigest()


def test_startup_cleanup_waits_for_upload_transaction_before_deciding_file_is_orphan(core):
    row, data = staged_original(core)
    image_spool.discard(core.store, [row["item_id"]])
    core.store.execute("UPDATE content_items SET state='FETCHING'")
    started, finished, failures = threading.Event(), threading.Event(), []

    def startup_cleanup():
        other = Store(core.store.path)
        try:
            started.set()
            image_spool.cleanup(other, core.clock())
        except BaseException as error:
            failures.append(error)
        finally:
            other.close()
            finished.set()

    with core.store.transaction():
        image_spool.put(core.store, row, data, "image/png", after(core.clock(), 300))
        core.store.execute("UPDATE content_items SET state='QUEUED'")
        worker = threading.Thread(target=startup_cleanup)
        worker.start()
        assert started.wait(timeout=5)
        assert not finished.wait(timeout=0.05)
    worker.join(timeout=5)
    assert finished.is_set() and not failures
    assert image_spool.get(core.store, row, core.clock(), 50000) == (data, "image/png")


@pytest.mark.parametrize("failure", [
    "expired", "missing", "oversize", "epoch", "truncated", "extended_expiry", "invalid_expiry",
])
def test_original_cache_refuses_incomplete_changed_or_expired_payload(core, failure):
    row, _ = staged_original(core)
    path = next(image_spool.directory(core.store).glob("*.raw"))
    if failure == "expired":
        core.clock.advance(301)
    elif failure == "missing":
        path.unlink()
    elif failure == "oversize":
        path.write_bytes(b"x" * 51000)
    elif failure == "epoch":
        row = {**row, "captured_privacy_epoch": 1}
    elif failure in {"extended_expiry", "invalid_expiry"}:
        header, data = path.read_bytes().split(b"\n", 1)
        value = json.loads(header)
        value["expires_at"] = (
            after(core.clock(), 1000) if failure == "extended_expiry" else "2099-01-01"
        )
        path.write_bytes(encode(value).encode() + b"\n" + data)
    else:
        path.write_bytes(b'{"mime":')
    with pytest.raises(CoreError, match="ORIGINAL_UNAVAILABLE"):
        image_spool.get(core.store, row, core.clock(), 50000)


def test_failed_original_write_leaves_no_partial_file(core, monkeypatch):
    row, data = staged_original(core)
    image_spool.discard(core.store, [row["item_id"]])

    def failed_sync(descriptor):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(image_spool.os, "fsync", failed_sync)
    with pytest.raises(OSError, match="synthetic disk failure"):
        image_spool.put(core.store, row, data, "image/png", after(core.clock(), 300))
    assert list(image_spool.directory(core.store).glob("*.raw")) == []


@pytest.mark.parametrize("reason", [
    "orphan", "job_done", "failed", "expired", "erased", "global_epoch",
])
def test_recovery_removes_abandoned_or_finished_originals(core, reason):
    row, _ = staged_original(core)
    if reason == "orphan":
        core.store.execute("DELETE FROM content_items")
    elif reason == "job_done":
        core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    elif reason == "failed":
        core.store.execute("UPDATE content_items SET state='FAILED'")
    elif reason == "expired":
        core.clock.advance(301)
    elif reason == "global_epoch":
        advance_epoch(core.store)
    else:
        with core.store.transaction():
            core.ledger.expire_events(core.store, [row["owner_event_id"]], core.clock())
    image_spool.cleanup(core.store, core.clock())
    assert list(image_spool.directory(core.store).glob("*.raw")) == []


def test_stop_discards_only_images_from_the_matching_window(core):
    row, _ = staged_original(core)
    core.store.insert(
        "runtime_meta", key="live_text_input:" + row["owner_event_id"],
        value=json.dumps({"window_sha256": "a" * 64}),
    )
    core.store.insert("runtime_meta", key="live_text_image_upload:" + row["item_id"], value="{}")
    image_spool.discard_window(core.store, "b" * 64)
    assert len(list(image_spool.directory(core.store).glob("*.raw"))) == 1
    image_spool.discard_window(core.store, "a" * 64)
    assert list(image_spool.directory(core.store).glob("*.raw")) == []
    assert not core.store.one(
        "SELECT 1 FROM runtime_meta WHERE key LIKE 'live_text_image_upload:%'",
    )


def test_erasure_replay_also_removes_copied_originals_from_restored_database(core, tmp_path):
    row, _ = staged_original(core)
    key = Fernet.generate_key()
    archive, destination = tmp_path / "original.enc", tmp_path / "restore.db"
    backup(core.settings.database, archive, key)
    cached = image_spool.directory(core.store)
    restored_cache = destination.parent / (destination.name + ".owner-images")
    shutil.copytree(cached, restored_cache)
    with core.store.transaction():
        core.ledger.expire_events(core.store, [row["owner_event_id"]], core.clock())
    assert list(cached.glob("*.raw")) == []
    restore(archive, destination, key, core.settings.erasure_ledger)
    assert list(restored_cache.glob("*.raw")) == []
    restored = Core(
        core.settings.model_copy(update={"database": str(destination)}), clock=core.clock,
    )
    try:
        assert restored.store.one("SELECT state FROM content_items")["state"] == "ERASED"
        assert list(restored_cache.glob("*.raw")) == []
    finally:
        restored.close()

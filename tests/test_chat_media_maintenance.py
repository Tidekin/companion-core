"""Foreground claims do not scan spools; maintenance remains bounded and fenced."""

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

import pytest

from companion import image_generation, image_spool
from companion.store import Store, after
from tests.support.image_originals import staged_original


def test_claim_does_not_scan_or_read_media_directories(core, monkeypatch):
    row, data = staged_original(core)

    def forbidden(*args, **kwargs):
        raise AssertionError("foreground claim entered media maintenance")

    monkeypatch.setattr(image_spool, "cleanup", forbidden)
    monkeypatch.setattr(image_generation, "cleanup", forbidden)
    assert core.claim_job("foreground")["event_id"] == row["owner_event_id"]
    assert image_spool.get(core.store, row, core.clock(), 50000)[0] == data


def test_media_maintenance_progresses_in_bounded_batches(core):
    root = image_spool.directory(core.store)
    root.mkdir(mode=0o700)
    for number in range(97):
        (root / f"content_{number:032x}.raw").write_bytes(b"orphan")
    counts = []
    for expected in (65, 33, 1, 0):
        counts.append(core.cleanup_media())
        assert len(list(root.iterdir())) == expected
    assert counts == [32, 32, 32, 1]
    assert not core._media_scans
    # A later pass discovers files created after the previous scan ended.
    (root / ("content_" + "a" * 32 + ".raw")).write_bytes(b"late orphan")
    assert core.cleanup_media() == 1
    assert not list(root.iterdir())


def test_maintenance_checks_headers_without_reading_valid_payloads(core, monkeypatch):
    row, data = staged_original(core)

    def forbidden(*args, **kwargs):
        raise AssertionError("maintenance read a full media payload")

    monkeypatch.setattr(image_spool, "get", forbidden)
    assert core.cleanup_media() == 0
    assert image_spool.deadline(core.store, row) > core.clock()
    core.clock.advance(301)
    assert core.cleanup_media() == 1
    assert not list(image_spool.directory(core.store).iterdir())


def test_nonmatching_files_also_consume_the_maintenance_budget(core):
    root = image_spool.directory(core.store)
    root.mkdir(mode=0o700)
    for number in range(90):
        (root / f"unrecognized-{number}").write_bytes(b"retained")
    assert core.cleanup_media() == 0
    assert core._media_scans
    assert core.cleanup_media() == 0
    assert core._media_scans
    assert core.cleanup_media() == 0
    assert len(list(root.iterdir())) == 90
    assert not core._media_scans


def test_core_close_releases_a_partial_directory_scan(core):
    root = image_spool.directory(core.store)
    root.mkdir(mode=0o700)
    for number in range(40):
        (root / f"content_{number:032x}.raw").write_bytes(b"orphan")
    core.cleanup_media()
    assert core._media_scans
    core.close()
    assert not core._media_scans


def test_another_maintainer_can_remove_a_file_after_enumeration(core):
    row, _ = staged_original(core)
    with os.scandir(image_spool.directory(core.store)) as scan:
        entry = next(scan)
    os.unlink(entry.path)
    assert image_spool.cleanup(core.store, core.clock(), entries=iter([entry]), limit=32) == (1, 0)
    assert core.store.one('SELECT state FROM content_items WHERE item_id=%s',
                           (row["item_id"],))["state"] == "QUEUED"


def test_original_cleanup_rechecks_an_upload_after_it_commits(core, monkeypatch):
    row, data = staged_original(core)
    item = row["item_id"]
    image_spool.discard(core.store, [item])
    core.store.execute("UPDATE content_items SET state='WAITING' WHERE item_id=%s", (item,))
    inspected = Event()
    with (closing(Store(core.database, core.settings.state_directory)) as cleaner,
          ThreadPoolExecutor(max_workers=1) as pool):
        read = cleaner.one

        def observe(sql, *args):
            result = read(sql, *args)
            if result and result.get("state") == "WAITING":
                inspected.set()
            return result

        monkeypatch.setattr(cleaner, "one", observe)
        with core.store.transaction():
            # Upload publishes the bytes before its QUEUED state is visible to another session.
            image_spool.put(core.store, row, data, "image/png", after(core.clock(), 300))
            core.store.execute("UPDATE content_items SET state='QUEUED' WHERE item_id=%s", (item,))
            scan = pool.submit(image_spool.cleanup, cleaner, core.clock())
            assert inspected.wait(5)
        assert scan.result(timeout=8) == (1, 0)
    assert image_spool.get(core.store, row, core.clock(), len(data))[0] == data


def test_original_cleanup_requires_exclusive_control_inside_an_existing_transaction(core):
    row, _ = staged_original(core)
    core.clock.advance(301)
    with (core.store.transaction(),
          pytest.raises(RuntimeError, match="ERASURE_CONTROL_WRITE_REQUIRED")):
        image_spool.cleanup(core.store, core.clock())
    assert (image_spool.directory(core.store) / (row["item_id"] + ".raw")).exists()
    with core.store.transaction(control_write=True):
        assert image_spool.cleanup(core.store, core.clock()) == (1, 1)


def test_generated_cleanup_counts_only_successful_unlinks(core):
    root = image_generation.directory(core.store)
    root.mkdir(mode=0o700, exist_ok=True)
    stray = root / "orphan.tmp"
    stray.write_bytes(b"orphan")
    with os.scandir(root) as scan:
        entry = next(scan)
    assert image_generation.cleanup(core.store, core.clock(), entries=[entry]) == (1, 1)
    assert image_generation.cleanup(core.store, core.clock(), entries=[entry]) == (1, 0)

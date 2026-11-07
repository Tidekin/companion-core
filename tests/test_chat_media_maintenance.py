"""Foreground claims do not scan spools; maintenance remains bounded and fenced."""

import os

from companion import image_generation, image_spool
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
    assert core.cleanup_media() == 1
    assert image_spool.deadline(core.store, row) > core.clock()
    core.clock.advance(301)
    assert core.cleanup_media() == 1
    assert not list(image_spool.directory(core.store).iterdir())


def test_nonmatching_files_also_consume_the_maintenance_budget(core):
    root = image_spool.directory(core.store)
    root.mkdir(mode=0o700)
    for number in range(90):
        (root / f"unrecognized-{number}").write_bytes(b"retained")
    assert core.cleanup_media() == 32
    assert core.cleanup_media() == 32
    assert core.cleanup_media() == 26
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
    assert image_spool.cleanup(core.store, core.clock(), entries=iter([entry]), limit=32) == 1
    assert core.store.one('SELECT state FROM content_items WHERE item_id=%s',
                           (row["item_id"],))["state"] == "QUEUED"

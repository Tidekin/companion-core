import hashlib
import sqlite3

import pytest

from companion_onebot_bridge.meme_catalog import MemeCatalog
from companion_onebot_bridge.meme_library import MemeLibrary
from tests.support.next_memes_catalog import check, metadata, png


@pytest.fixture
def catalog(tmp_path):
    db = sqlite3.connect(tmp_path / "spool.sqlite3")
    instance = MemeCatalog(db, MemeLibrary(tmp_path, enabled=True), clock=lambda: 10000)
    yield instance
    db.close()


def offer(catalog, key="first", **kwargs):
    return catalog.candidates(key, "celebrate", check=check, **kwargs)


def pin(catalog, key="first"):
    candidates = offer(catalog, key)
    return catalog.prepare_selection(key, candidates[0]["candidate_id"], check=check)


def test_semantic_positive_negative_and_model_cannot_choose_unoffered_id(catalog):
    good = catalog.add(png(), metadata(category="sad", description="安慰 hug 抱抱",
                                     tags=("comfort",), suitable=("comfort",)), check=check)
    catalog.add(png("blue"), metadata(category="sad", description="哈哈，嘲笑，抱抱",
                                     tags=("comfort",), suitable=("comfort",)), check=check)
    catalog.add(png("red"), metadata(description="安慰 抱抱", suitable=("comfort",),
                                    avoid=("comfort",)), check=check)
    candidates = catalog.candidates("first", "comfort", category="sad", check=check)
    assert [r["candidate_id"] for r in candidates] == [good]
    with pytest.raises(ValueError, match="NOT_OFFERED"):
        catalog.prepare_selection("first", "0" * 64, check=check)
    assert catalog.prepare_selection("first", good, check=check).sha256 == good


def test_candidates_bounded_no_match_and_no_raw_private_query(catalog):
    for color in range(12):
        catalog.add(png((color, 0, 0)), metadata(), check=check)
    assert len(offer(catalog, limit=3)) == 3
    assert len(offer(catalog, limit=8)) == 3  # frozen offer cannot expand on a retry
    assert catalog.candidates("night", "sleep", check=check) == []
    assert catalog.prepare_selection("night", None, check=check) is None
    with pytest.raises(ValueError, match="INTENT_INVALID"):
        catalog.candidates("private", "my owner's private message", check=check)
    with pytest.raises(ValueError, match="OFFER_CONFLICT"):
        catalog.candidates("first", "like", check=check)


def test_immutable_bytes_and_selection_survive_restart(tmp_path, catalog):
    data = png()
    source = tmp_path / "happy/a.png"
    source.parent.mkdir()
    source.write_bytes(data)
    sha = catalog.add_local(source, metadata(), check=check)
    assert pin(catalog).data == data
    source.write_bytes(png("red"))
    catalog.add_local(source, metadata(), check=check)
    source.unlink()
    restarted = sqlite3.connect(tmp_path / "spool.sqlite3")
    try:
        second = MemeCatalog(restarted, catalog.library)
        image = second.prepare_selection("first", sha, check=check)
        assert image.data == data and image.sha256 == hashlib.sha256(data).hexdigest()
        with pytest.raises(ValueError, match="SELECTION_CONFLICT"):
            second.prepare_selection("first", hashlib.sha256(png("red")).hexdigest(), check=check)
    finally:
        restarted.close()


def test_success_cooldown_is_persistent_and_ack_replay_does_not_extend(catalog, tmp_path):
    catalog.add(png(), metadata(), check=check)
    selected = pin(catalog)
    catalog.mark_sending("first", check=check)
    catalog.receipt("first", "SENT")
    restarted = sqlite3.connect(tmp_path / "spool.sqlite3")
    try:
        second = MemeCatalog(restarted, catalog.library, clock=lambda: 11000)
        assert offer(second, "cooldown") == []
        second.receipt("first", "SENT")
        second.clock = lambda: 11801
        assert offer(second, "after")[0]["candidate_id"] == selected.sha256
        assert second.prepare_selection("first", selected.sha256, check=check) is None
    finally:
        restarted.close()


@pytest.mark.parametrize("report_unknown", [False, True])
def test_inflight_or_unknown_is_held_indefinitely(catalog, report_unknown):
    catalog.add(png(), metadata(), check=check)
    pin(catalog)
    catalog.mark_sending("first", check=check)
    if report_unknown:
        catalog.receipt("first", "UNKNOWN")
    catalog.clock = lambda: 1e12
    assert offer(catalog, "years_later") == []
    with pytest.raises(ValueError, match="RECEIPT_CONFLICT"):
        catalog.receipt("first", "NOT_SENT")
    catalog.receipt("first", "SENT")  # explicit late confirmed receipt resolves the hold
    assert offer(catalog, "confirmed_cooldown") == []


def test_hash_duplicate_is_single_asset_and_removal_never_reactivates(catalog):
    sha = catalog.add(png(), metadata(), check=check)
    assert catalog.add(png(), metadata(category="like"), check=check) == sha
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 1
    pin(catalog)
    catalog.remove(sha)
    catalog.add(png(), metadata(), check=check)
    assert catalog.prepare_selection("first", sha, check=check) is None
    assert offer(catalog, "new") == []
    with pytest.raises(ValueError, match="SEND_FENCED"):
        catalog.mark_sending("first", check=check)


@pytest.mark.parametrize("stage", ["before_offer", "offered", "prepared", "sending"])
def test_cancel_or_recall_fences_every_stage(catalog, stage):
    sha = catalog.add(png(), metadata(), check=check)
    if stage != "before_offer":
        offer(catalog)
    if stage in {"prepared", "sending"}:
        catalog.prepare_selection("first", sha, check=check)
    if stage == "sending":
        catalog.mark_sending("first", check=check)
    catalog.cancel("first")
    catalog.cancel("first")
    assert offer(catalog) == []
    with pytest.raises(ValueError, match="SEND_FENCED"):
        catalog.mark_sending("first", check=check)
    assert bool(offer(catalog, "other")) == (stage != "sending")


def test_new_reply_generation_guard_prevents_prepare_and_send(catalog):
    sha = catalog.add(png(), metadata(), check=check)
    offer(catalog)

    def stale():
        raise ValueError("REPLY_SUPERSEDED")

    with pytest.raises(ValueError, match="SUPERSEDED"):
        catalog.prepare_selection("first", sha, check=stale)
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_pins").fetchone()[0] == 0
    catalog.prepare_selection("first", sha, check=check)
    with pytest.raises(ValueError, match="SUPERSEDED"):
        catalog.mark_sending("first", check=stale)
    assert catalog.db.execute("SELECT state FROM meme_pins").fetchone()[0] == "PREPARED"


def test_two_workers_cannot_pin_same_hash_and_loser_cannot_switch(catalog, tmp_path):
    sha = catalog.add(png(), metadata(), check=check)
    offer(catalog, "one")
    offer(catalog, "two")
    catalog.prepare_selection("one", sha, check=check)
    db = sqlite3.connect(tmp_path / "spool.sqlite3")
    try:
        second = MemeCatalog(db, catalog.library)
        assert second.prepare_selection("two", sha, check=check) is None
        with pytest.raises(ValueError, match="SELECTION_CONFLICT"):
            second.prepare_selection("two", None, check=check)
    finally:
        db.close()


def test_storage_binding_disabled_and_transaction_failure(catalog):
    catalog.max_storage_bytes = 1
    with pytest.raises(ValueError, match="STORAGE_LIMIT"):
        catalog.add(png(), metadata(), check=check)
    catalog.max_storage_bytes = 10000
    catalog.db.execute("CREATE TRIGGER fail_import BEFORE INSERT ON meme_assets "
                       "BEGIN SELECT RAISE(ABORT, 'synthetic disk failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        catalog.add(png(), metadata(), check=check)
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 0
    catalog.db.execute("DROP TRIGGER fail_import")
    catalog.add(png(), metadata(), check=check)
    catalog.library.enabled = False
    assert offer(catalog) == []
    catalog.library.enabled = True
    catalog.max_bindings = 1
    offer(catalog)
    with pytest.raises(ValueError, match="BINDING_LIMIT"):
        offer(catalog, "overflow")


def test_local_import_keeps_existing_file_and_media_guards(catalog, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.png")
    # Do not create external files: path traversal is rejected before bytes are opened.
    with pytest.raises(ValueError, match="ASSET_INVALID"):
        catalog.add_local(outside, metadata(), check=check)
    catalog.library.max_dimension = 16
    with pytest.raises(ValueError, match="ASSET_INVALID"):
        catalog.add(png(), metadata(), check=check)


def test_revocation_before_transaction_commit_rolls_back_asset(catalog):
    calls = 0

    def revoke():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("RECALLED")

    with pytest.raises(ValueError, match="RECALLED"):
        catalog.add(png(), metadata(), check=revoke)
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 0

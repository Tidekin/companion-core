"""Signed WS to independent API/worker to original OneBot delivery, with synthetic I/O."""

import json

import httpx
import pytest

from tests.support.meme_semantic_integration import check_signed_caption_and_delivery


@pytest.mark.parametrize("handoff,deferred,delayed", [
    (False, False, False), (True, False, False), (False, True, False),
    (False, False, True),
], ids=["ordinary", "cancelled-before-usage", "query-unavailable", "delayed-authority-hops"])
def test_signed_capture_caption_index_current_offer_and_original_image_delivery(
    core, tmp_path, handoff, deferred, delayed, explicit=False, segmented=False, segment_cap=3,
):
    return check_signed_caption_and_delivery(
        core,
        tmp_path,
        handoff,
        deferred,
        delayed,
        explicit,
        segmented,
        segment_cap,
    )


def test_explicit_request_uses_native_image_offer_without_spontaneous_sampling(core, tmp_path):
    test_signed_capture_caption_index_current_offer_and_original_image_delivery(
        core, tmp_path, handoff=False, deferred=False, delayed=False, explicit=True)


@pytest.mark.parametrize("cap", [2, 3])
def test_native_meme_text_bubbles_then_original_image_keep_visible_count_cap(core, tmp_path, cap):
    test_signed_capture_caption_index_current_offer_and_original_image_delivery(
        core, tmp_path, handoff=False, deferred=False, delayed=False,
        explicit=True, segmented=True, segment_cap=cap)


@pytest.mark.parametrize("boundary", ["missing", "stop"])
def test_native_image_tail_never_sends_empty_or_replays_text(core, tmp_path, monkeypatch, boundary):
    from companion import owner_authority
    from companion.owner_authority_protocol import AuthorityStop
    from companion_onebot_bridge import reply_segments
    from companion_onebot_bridge.owner_send import OwnerOneBot

    original = OwnerOneBot.prepare_delivery
    intercepted = []

    async def prepare(self, item):
        if reply_segments.image_only(item):
            intercepted.append(item["message_id"])
            if boundary == "missing":
                db = self.guard.runtime.memes.catalog.db
                with db:
                    db.execute("UPDATE meme_assets SET active=0 WHERE sha=?",
                               (item["content"]["image"]["candidate_id"],))
            else:
                authority = owner_authority.current(core.store, core.settings, core.clock).authority
                owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                    operation_id="meme-tail-stop", authority=authority.as_dict()))
        return await original(self, item)

    monkeypatch.setattr(OwnerOneBot, "prepare_delivery", prepare)
    error = ValueError if boundary == "missing" else httpx.HTTPStatusError
    with pytest.raises(error):
        test_signed_capture_caption_index_current_offer_and_original_image_delivery(
            core, tmp_path, handoff=False, deferred=False, delayed=False,
            explicit=True, segmented=True)
    assert len(intercepted) == 1
    rows = core.store.rows("SELECT state,attempt_count,content_json FROM outbox ORDER BY rowid")
    assert [row["state"] for row in rows[:-1]] == ["SENT", "SENT", "SENT"]
    assert rows[-1]["state"] == ("LEASED" if boundary == "missing" else "CANCELLED")
    assert rows[-1]["attempt_count"] == 0
    assert json.loads(rows[-1]["content_json"])["text"] == ""

"""Existing Life source consumers agree on evidence without scheduling or generating.

Synthetic sources only. These checks do not implement topic closure or SHADOW generation.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from companion.api import create_app
from companion.core import Core
from tests.conftest import ingest
from tests.support.next_memory_context import context, episode
from tests.support.p12_diary_life import source_with_older_parent


def local_day(core, event_id):
    row = core.store.one('SELECT occurred_at FROM events WHERE event_id=%s', (event_id,))
    return datetime.fromisoformat(row["occurred_at"]).astimezone(
        ZoneInfo(core.settings.timezone),
    ).date()


def test_private_experience_can_supply_diary_and_continuation_but_not_reading_share(core):
    view = context(core)
    ingest(core, 1, "今天给松林项目画了草图")
    episode(core, 1)
    conversation = core.store.one("SELECT conversation_id FROM events")["conversation_id"]
    with core.store.read_snapshot():
        assert view.continuation(conversation, core.clock())["items"][0]["excerpts"][0][
            "event_id"
        ] == "evt_test_1"
        assert core.diary.repo.sources(local_day(core, "evt_test_1"))[0]["event_id"] == "evt_test_1"
        assert core.social.preview("evt_test_1", core.settings.owner_qq_id) is None
    assert not core.model.calls
    assert not core.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("invalidate", ["expiry", "withdrawal", "restart"])
def test_reading_preview_and_diary_share_current_evidence_without_new_work(core, invalidate):
    source = source_with_older_parent(core)
    day = local_day(core, source)
    core.settings.control_token = SecretStr("synthetic-life-preview-control-" * 3)
    app = create_app(core.settings)
    app.state.core = core
    client = TestClient(app)
    path = f"/internal/v2/social/{source}/preview"
    control = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
    bridge = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
    with core.store.read_snapshot():
        assert client.get(path, headers=bridge).status_code == 401
        preview = client.get(path, headers=control)
        assert preview.status_code == 200 and preview.headers["Cache-Control"] == "no-store"
        draft = preview.json()["draft"]
        evidence = next(row for row in core.diary.repo.sources(day) if row["event_id"] == source)
        assert draft["event_id"] == evidence["event_id"]
        assert draft["evidence_level"] == evidence["evidence_level"] == "FETCHED"
        assert draft["auto_share"] is False

    if invalidate == "restart":
        with Core(core.settings, core.model, core.clock) as restarted:
            assert restarted.social.preview(source, core.settings.owner_qq_id) == draft
            assert evidence in restarted.diary.repo.sources(day)
    else:
        if invalidate == "expiry":
            core.clock.advance(2)  # Original owner ancestor expires before its newer reading.
        else:
            with core.store.transaction(control_write=True):
                core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
        after = client.get(path, headers=control)
        assert after.status_code == 200 and after.json() == {"draft": None}
        assert all(row["event_id"] != source for row in core.diary.repo.sources(day))
    assert not core.model.calls
    for table in ("outbox", "model_usage", "social_candidates", "diary_runs"):
        assert not core.store.rows("SELECT * FROM " + table)

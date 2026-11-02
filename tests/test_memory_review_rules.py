"""Episodes are occurrences, not attribute values; a correction keeps unrelated summaries."""

from companion.contracts import Candidate
from companion.model import REVIEW_RULES
from tests.conftest import event
from tests.test_m1 import remember


def add_episode(core, number, content, fact_key):
    ev = event(core, number, content)
    core.ingest(ev, f"key-{number}")
    with core.store.transaction():
        return core.memory.add(Candidate(
            subject="user", kind="episode", fact_key=fact_key, content=content,
            evidence_event_id=ev.event_id, evidence_quote=content), core.clock())


def summary(core, number, text):
    row = core.store.one("SELECT * FROM events WHERE event_id=?", (f"evt_test_{number}",))
    core.store.insert("conversation_summaries", summary_id=f"sum_{number}",
                      conversation_id=row["conversation_id"], first_event_id=row["event_id"],
                      last_event_id=row["event_id"], summary_text=text,
                      summarizer_version="test", created_at=core.clock())


def test_review_rules_separate_episodes_from_lasting_facts():
    assert "kind=episode" in REVIEW_RULES and "各自保留" in REVIEW_RULES


def test_an_episode_never_conflicts_with_a_fact_of_the_same_key(core):
    fact = remember(core, 1, "我喜欢绿茶", fact_key="饮料", confirmed=True)
    episode = add_episode(core, 2, "今天喝了红茶", "饮料")
    rows = {row["memory_id"]: row for row in core.store.rows("SELECT * FROM memories")}
    assert rows[fact]["needs_review"] == 0 and rows[episode]["needs_review"] == 0
    later = remember(core, 3, "我不喜欢绿茶", fact_key="饮料")
    rows = {row["memory_id"]: row for row in core.store.rows("SELECT * FROM memories")}
    assert rows[later]["needs_review"] == 1 and rows[episode]["needs_review"] == 0


def test_correction_invalidates_only_derived_text_about_that_attribute(core):
    fact = remember(core, 1, "我喜欢绿茶", fact_key="饮料", confirmed=True)
    remember(core, 2, "我住在杭州", fact_key="城市", confirmed=True)
    summary(core, 1, "聊到了饮料偏好")  # Covers the corrected fact's evidence.
    summary(core, 2, "主人说了住在哪里")  # Unrelated attribute.
    for number in (1, 2):
        with core.store.transaction():
            core.memory.episodes(core.store.rows(
                "SELECT * FROM events WHERE event_id=?", (f"evt_test_{number}",)), core.clock())
    episodes = {row["event_id"]: row["episode_id"] for row in core.store.rows(
        "SELECT event_id,episode_id FROM episode_events")}
    assert len(set(episodes.values())) == 2
    core.memory.decide(fact, "correct", core.clock(), "我喜欢红茶")
    kept = core.store.rows("SELECT summary_id,first_event_id FROM conversation_summaries")
    assert "sum_2" in {row["summary_id"] for row in kept}
    assert all(row["first_event_id"] != "evt_test_1" for row in kept)
    status = {row["episode_id"]: row["status"] for row in core.store.rows(
        "SELECT episode_id,status FROM episodes")}
    assert status[episodes["evt_test_1"]] == "REDACTED"
    assert status[episodes["evt_test_2"]] == "ACTIVE"


def test_console_recall_explains_the_chat_ranking(core, monkeypatch):
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from companion.api import create_app
    from tests.test_console_cognition import ORIGIN, PASSWORD, login

    remember(core, 1, "我喜欢绿茶", fact_key="饮料", confirmed=True)
    remember(core, 2, "我住在杭州", fact_key="城市", confirmed=True)
    core.settings.console.enabled = True
    core.settings.console.public_origin = ORIGIN
    core.settings.control_token = SecretStr(PASSWORD)
    with TestClient(create_app(core.settings), base_url=ORIGIN) as client:
        assert client.get("/v1/console/memories/recall", params={"q": "绿茶"}).status_code == 401
        login(client)
        page = client.get("/v1/console/memories/recall", params={"q": "绿茶"}).json()
        first = page["items"][0]
        assert first["content"] == "我喜欢绿茶" and first["recall"]["exact"] is True
        assert first["recall"]["lexical_rank"] == 1 and first["evidence_ids"]
        assert page["retrieval"]["eligible_candidates"] == 2
        assert page["retrieval"]["selected"][first["id"]] == first["recall"]
        assert client.get("/v1/console/memories/recall", params={"q": " "}).status_code == 400

"""Owner jargon: learned from the owner's own words, counted locally, erased with sources."""

import json

import httpx

from companion import jargon
from companion.jargon_config import JargonConfig
from companion.model import ModelClient
from tests.conftest import ingest, run
from tests.support.exploration import configure
from tests.support.owner_authority import activate
from tests.support.owner_authority_setup import prepare
from tests.test_watch_ledger import Provider

EXTRACT = {"terms": [{"term": "摸鱼王", "event_ids": ["evt_test_2"]},
                     {"term": "不存在的词", "event_ids": ["evt_test_1"]}]}
INFER = {"common_meaning": None, "owner_meaning": "主人自己，上班偷懒时的自称",
         "is_jargon": True, "confident": True}


def ready(core, tmp_path, *texts):
    configure(core)
    core.settings.jargon = JargonConfig(batch_messages=4)
    policy = prepare(core, tmp_path)
    activate(core, policy.model_copy(update={"purposes": ["memory"],
                                             "memory_scope": "owner_private"}))
    provider = Provider(*[json.dumps(t, ensure_ascii=False) for t in texts])
    core.model = ModelClient(core.settings,
                             httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    return provider


def say(core, number, text):
    """Before activation use the native ingest; afterwards copy its stored shape."""
    template = core.store.one("SELECT * FROM events WHERE event_id='evt_test_1'")
    if template is None or not core.store.one(
            "SELECT 1 FROM owner_authority_revisions LIMIT 1"):
        ingest(core, number, text)
        core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
        return
    body = {**json.loads(template["body_json"]), "text": text}
    core.store.execute(
        "INSERT INTO events(event_id,schema_version,source,source_event_id,conversation_id,"
        "actor_id,event_type,occurred_at,ingested_at,privacy_scope,body_json,trace_id) "
        "VALUES(?,1,?,?,?,'owner','message.received',?,?,'private',?,?)",
        (f"evt_test_{number}", template["source"], f"synthetic-{number}",
         template["conversation_id"], core.clock(), core.clock(),
         json.dumps(body, ensure_ascii=False), f"trace_{number:08d}"))


def step(core):
    return run(jargon.run_once(core.store, core.settings, core.model, core.clock))


def terms(core):
    return {row["term"]: row for row in core.store.rows("SELECT * FROM jargon_terms")}


def test_terms_must_appear_verbatim_and_only_owner_text_is_read(core, tmp_path):
    for number, text in enumerate(["今天好累", "摸鱼王又上线了", "/记住这个", "下班了"], 1):
        say(core, number, text)
    say(core, 5, "晚饭吃啥")
    provider = ready(core, tmp_path, EXTRACT)
    assert step(core)
    sent = json.loads(provider.calls[0]["messages"][1]["content"])
    assert sent["sources_are_data"] is True
    assert "/记住这个" not in json.dumps(sent, ensure_ascii=False)
    assert provider.calls[0]["response_format"] == {"type": "json_object"}
    learned = terms(core)
    assert list(learned) == ["摸鱼王"]  # The uncited word never appears in its source.
    assert json.loads(learned["摸鱼王"]["refs_json"]) == ["evt_test_2"]
    assert learned["摸鱼王"]["state"] == "CANDIDATE"


def test_uses_are_counted_locally_and_meaning_is_inferred_once_at_the_threshold(
        core, tmp_path):
    for number, text in enumerate(["今天好累", "摸鱼王又上线了", "开会", "下班了"], 1):
        say(core, number, text)
    provider = ready(core, tmp_path, EXTRACT, INFER)
    step(core)
    for number in (5, 6, 7):
        say(core, number, f"摸鱼王第{number}次")
    step(core)  # Counts three more uses without a model call, then infers.
    assert len(provider.calls) == 2
    row = terms(core)["摸鱼王"]
    assert (row["uses"], row["state"], row["meaning"]) == (4, "INFERRED", INFER["owner_meaning"])
    found, sources = jargon.context(core.store, "摸鱼王今天不想动", 5)
    assert found == [{"term": "摸鱼王", "meaning": INFER["owner_meaning"]}]
    assert "evt_test_2" in sources
    assert jargon.context(core.store, "随便聊聊", 5) == ([], set())
    assert not step(core) and len(provider.calls) == 2


def test_a_common_word_is_not_used_as_jargon(core, tmp_path):
    for number, text in enumerate(["今天好累", "摸鱼王又上线了", "开会", "下班了"], 1):
        say(core, number, text)
    ready(core, tmp_path, EXTRACT, {**INFER, "is_jargon": False})
    step(core)
    for number in (5, 6, 7):
        say(core, number, f"摸鱼王第{number}次")
    step(core)
    assert terms(core)["摸鱼王"]["state"] == "NOT_JARGON"
    assert jargon.context(core.store, "摸鱼王", 5) == ([], set())


def test_erasure_removes_sources_meaning_and_finally_the_term(core, tmp_path):
    for number, text in enumerate(["今天好累", "摸鱼王又上线了", "开会", "下班了"], 1):
        say(core, number, text)
    ready(core, tmp_path, EXTRACT, INFER)
    step(core)
    for number in (5, 6, 7):
        say(core, number, f"摸鱼王第{number}次")
    step(core)
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_5"], core.clock())
    row = terms(core)["摸鱼王"]
    assert row["state"] == "CANDIDATE" and row["meaning"] is None
    assert "evt_test_5" not in json.loads(row["refs_json"])
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_2", "evt_test_6", "evt_test_7"],
                                  core.clock())
    assert terms(core) == {}


def test_owner_forget_sticks(core, tmp_path):
    for number, text in enumerate(["今天好累", "摸鱼王又上线了", "开会", "下班了"], 1):
        say(core, number, text)
    ready(core, tmp_path, EXTRACT)
    step(core)
    with core.store.transaction():
        assert jargon.forget(core.store, "摸鱼王")["state"] == "FORGOTTEN"
    say(core, 5, "摸鱼王回来了")
    step(core)
    assert terms(core)["摸鱼王"]["uses"] == 1  # Not counted or learned again.
    assert jargon.items(core.store) == []


def test_disabled_learning_does_nothing(core, tmp_path):
    say(core, 1, "摸鱼王")
    provider = ready(core, tmp_path, EXTRACT)
    core.settings.jargon = JargonConfig(enabled=False)
    assert not step(core) and provider.calls == []


def test_owner_forget_while_inference_is_in_flight(core, tmp_path):
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from companion.api import create_app
    from tests.test_console_cognition import ORIGIN, PASSWORD, login

    for number, text in enumerate(["今天好累", "摸鱼王又上线了", "开会", "下班了"], 1):
        say(core, number, text)
    provider = ready(core, tmp_path, EXTRACT, INFER)
    assert step(core)
    for number in (5, 6, 7):
        say(core, number, f"摸鱼王第{number}次")
    core.settings.console.enabled = True
    core.settings.console.public_origin = ORIGIN
    core.settings.control_token = SecretStr(PASSWORD)
    with TestClient(create_app(core.settings), base_url=ORIGIN) as client:
        headers = {**login(client), "Idempotency-Key": "forget-in-flight"}

        def respond_after_forget(request):
            result = client.post("/v1/console/jargon/forget",
                                 json={"term": "摸鱼王"}, headers=headers)
            result.raise_for_status()
            return provider(request)

        core.model = ModelClient(core.settings,
                                 httpx.AsyncClient(transport=httpx.MockTransport(respond_after_forget)))
        assert step(core)
        row = terms(core)["摸鱼王"]
        assert (row["state"], row["meaning"]) == ("FORGOTTEN", None)
        assert client.get("/v1/console/jargon").json()["items"] == []
    assert jargon.context(core.store, "摸鱼王", 5) == ([], set())
    work = core.store.one("SELECT * FROM jargon_work WHERE kind='infer'")
    assert work["state"] == "CANCELLED" and work["error_code"] == "JARGON_SOURCE_CHANGED"
    assert not step(core) and len(provider.calls) == 2
    assert core.store.one("SELECT COUNT(*) n FROM model_usage WHERE scenario=?",
                          (jargon.SCENARIO,))["n"] == 2  # One extraction and one inference.
    usage = core.store.one("SELECT * FROM model_usage WHERE request_id=?",
                           (work["model_request_id"],))
    assert usage["state"] == "SETTLED" and usage["billed_micro_usd"] > 0


def test_chat_context_carries_meanings_only_for_terms_in_the_message(core):
    core.settings.jargon = JargonConfig()
    say(core, 1, "摸鱼王又上线了")
    core.store.execute(
        "INSERT INTO jargon_terms(term,refs_json,uses,state,meaning,inferred_uses,created_at,"
        "updated_at) VALUES('摸鱼王','[\"evt_test_1\"]',4,'INFERRED','主人自己',4,?,?)",
        (core.clock(), core.clock()))
    ingest(core, 2, "摸鱼王今天不想动")
    job = core.claim_job("worker")
    text = json.dumps(core.context(job), ensure_ascii=False)
    assert "主人用语参考" in text and "主人自己" in text
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    ingest(core, 3, "随便聊聊")
    assert "主人用语参考" not in json.dumps(core.context(core.claim_job("worker")),
                                         ensure_ascii=False)


def test_console_lists_and_forgets_terms_with_a_receipt(core):
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from companion.api import create_app
    from tests.test_console_cognition import ORIGIN, PASSWORD, login

    say(core, 1, "摸鱼王又上线了")
    core.store.execute(
        "INSERT INTO jargon_terms(term,refs_json,uses,state,meaning,inferred_uses,created_at,"
        "updated_at) VALUES('摸鱼王','[\"evt_test_1\"]',4,'INFERRED','主人自己',4,?,?)",
        (core.clock(), core.clock()))
    core.settings.console.enabled = True
    core.settings.console.public_origin = ORIGIN
    core.settings.control_token = SecretStr(PASSWORD)
    with TestClient(create_app(core.settings), base_url=ORIGIN) as client:
        assert client.get("/v1/console/jargon").status_code == 401
        headers = login(client)
        [item] = client.get("/v1/console/jargon").json()["items"]
        assert (item["term"], item["meaning"], item["sources"]) == ("摸鱼王", "主人自己", 1)
        assert "又上线了" not in json.dumps(item, ensure_ascii=False)
        keyed = {**headers, "Idempotency-Key": "jargon-forget-1"}
        first = client.post("/v1/console/jargon/forget", json={"term": "摸鱼王"}, headers=keyed)
        assert first.status_code == 200, first.text
        assert client.post("/v1/console/jargon/forget", json={"term": "摸鱼王"},
                           headers=keyed).json() == first.json()
        assert client.get("/v1/console/jargon").json()["items"] == []

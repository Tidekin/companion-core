import json
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from PIL import Image
from pydantic import ValidationError

from astrbot_plugin_companion_bridge.expression_adapter import MemeLibrary
from companion.backup import backup, restore
from companion.contracts import CoreError, DeliveryResult
from companion.conversation_policy import (
    EVERY_TEXT_REQUIRED,
    TEXT_RELAXED,
    TEXT_REQUIRED,
    apply_strategy,
)
from companion.core import Core
from companion.expression import ExpressionEngine, ImageIntent, ResponseIntent
from companion.store import Store
from companion.transport import begin_send, delivery_result, digest
from tests.conftest import ingest, run
from tests.support.astrbot_delivery import deliver_expression, journal
from tests.support.basic_chat import lease


def engine(core, enabled=True):
    return ExpressionEngine(
        core.store,
        core.settings.model_copy(update={
            "expression_enabled": enabled, "expression_cooldown_minutes": 30,
        }),
        core.clock,
    )


def queued(core, number=1, text="做到了", reply="真不错。⬡happy⬡"):
    ingest(core, number, text)
    event_id = f"evt_test_{number}"
    conversation = core.store.one(
        'SELECT conversation_id FROM events WHERE event_id=%s', (event_id,)
    )
    message_id = engine(core).enqueue(
        event_id, conversation["conversation_id"], reply, core.clock()
    )
    return core.store.one('SELECT * FROM outbox WHERE message_id=%s', (message_id,))


def picture(root, name="happy/a.png", size=(32, 24), color="green"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def image_intent(category="happy"):
    return {"source": "astrbot.meme_manager", "category": category}


def test_expression_off_and_strict_intent_keep_text_contract(core):
    assert engine(core, False).instruction() == ""
    assert engine(core, False).intent("missing", "完整文字").model_dump(exclude_none=True) == {
        "text": "完整文字"
    }
    with pytest.raises(ValidationError):
        ImageIntent(category="../private")
    with pytest.raises(ValidationError):
        ImageIntent(category="happy", source="https://example.com/image.png")
    with pytest.raises(ValidationError):
        ResponseIntent(text="", image=ImageIntent(category="happy"))
    with pytest.raises(ValidationError):
        ImageIntent(category="happy", path="private.png")


def test_expression_freezes_one_intent_under_existing_delivery_key(core):
    row = queued(core)
    content = json.loads(row["content_json"])
    assert content == {"text": "真不错。", "image": image_intent()}
    assert row["content_sha256"] == digest(content)
    assert core.model.calls == []
    repeated = engine(core).enqueue(
        row["origin_event_id"], row["conversation_id"], "不同的回复。⬡like⬡", core.clock()
    )
    assert repeated == row["message_id"]
    assert core.store.one("SELECT content_json FROM outbox")["content_json"] == row["content_json"]
    assert "表情和语气选择不是情绪记录或事实依据" in engine(core).instruction()


def test_participation_strategies_relax_the_real_text_requirement(core):
    rules = engine(core).instruction_parts()[0]
    assert EVERY_TEXT_REQUIRED in rules
    relaxed = apply_strategy([{"role": "system", "content": rules}], "participation_v2")
    assert TEXT_REQUIRED not in relaxed[0]["content"]
    assert TEXT_RELAXED in relaxed[0]["content"]


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("普通文字", "普通文字"),
        ("这是回答。⬡unknown⬡", "这是回答。"),
        ("这是⬡happy⬡回答。", "这是回答。"),
        ("这是回答。⬡happy⬡⬡like⬡", "这是回答。"),
        ("这是回答。⬡../../private⬡", "这是回答。"),
    ],
)
def test_invalid_or_multiple_markers_degrade_to_original_text(core, reply, expected):
    ingest(core)
    intent = engine(core).intent("evt_test_1", reply)
    assert intent.text == expected and intent.image is None
    with pytest.raises(CoreError, match="EXPRESSION_TEXT_REQUIRED"):
        engine(core).intent("evt_test_1", "⬡happy⬡")


@pytest.mark.parametrize(
    "text",
    ["我今天很难过", "家人住院了", "关于自残", "讨论法律咨询", "先不要发任何表情", "a funeral"],
)
def test_serious_current_context_suppresses_even_a_model_selected_meme(core, text):
    row = queued(core, text=text)
    assert json.loads(row["content_json"]) == {"text": "真不错。"}


def test_serious_recent_context_and_reply_are_both_checked(core):
    ingest(core, 1, "我很难过")
    ingest(core, 2, "嗯")
    assert engine(core).intent("evt_test_2", "我在听。⬡happy⬡").image is None
    for number in range(3, 9):
        ingest(core, number, "最近在玩什么")
    assert engine(core).intent("evt_test_8", "我在听。⬡happy⬡").image is not None
    assert engine(core).intent("evt_test_8", "这个医疗问题需要认真处理。⬡happy⬡").image is None


def test_expression_control_external_and_cross_conversation_are_not_image_sources(core):
    ingest(core, 1, "/读日记 2026-10-18")
    assert engine(core).intent("evt_test_1", "日记内容。⬡happy⬡").image is None
    ingest(core, 2, "随便聊聊")
    core.store.execute("UPDATE events SET source='system.activity' WHERE event_id='evt_test_2'")
    assert engine(core).intent("evt_test_2", "内容。⬡happy⬡").image is None
    with pytest.raises(CoreError, match="EXPRESSION_CONVERSATION_MISMATCH"):
        engine(core).enqueue("evt_test_2", "wrong", "内容", core.clock())


@pytest.mark.parametrize("text,requested", [
    ("请发一张表情包", True),
    ("太棒了！发一张开心的表情包庆祝一下。", True),
    ("给我来个表情包", True),
    ("能不能发一个表情包", True),
    ("please send me a meme", True),
    ("太棒了，成功了", False),
    ("表情包挺好玩", False),
    ("他说‘发一个表情包’", False),
    ("不要给我发开心的表情包", False),
    ("严肃点，发一张表情包", False),
])
def test_only_direct_meme_requests_bypass_spontaneous_sampling(core, monkeypatch, text, requested):
    ingest(core, text=text)
    monkeypatch.setattr("companion.expression.digest", lambda value: "f" * 64)
    core.settings.expression_selection_probability = 0.35
    assert engine(core)._selection_allowed("evt_test_1", "meme:evt_test_1") is requested


@pytest.mark.parametrize("change", ["source", "actor", "privacy", "redacted", "control"])
def test_direct_meme_request_requires_current_owner_private_source(core, change):
    ingest(core, text="请发一张表情包")
    if change == "control":
        core.store.execute("UPDATE events SET body_json="
                           "jsonb_set(body_json::jsonb,'{control_command}','1')::text")
    else:
        field, value = {"source": ("source", "system.activity"),
                        "actor": ("actor_id", "system"), "privacy": ("privacy_scope", "public"),
                        "redacted": ("redacted", 1)}[change]
        core.store.execute("UPDATE events SET " + field + "=%s", (value,))
    core.settings.expression_selection_probability = 0
    assert not engine(core)._selection_allowed("evt_test_1", "meme:evt_test_1")


def test_outbox_cooldown_and_repeat_window_survive_restart_without_starvation(core):
    first = queued(core)
    # Pending and uncertain sends count too: neither can establish non-delivery.
    core.store.execute(
        "UPDATE outbox SET state='DELIVERY_UNCERTAIN' WHERE message_id=%s", (first["message_id"],)
    )
    second = queued(core, 2, reply="另一个好消息。⬡like⬡")
    assert "image" not in json.loads(second["content_json"])
    core.clock.advance(1801)
    reopened = Store(core.store.database, core.settings.state_directory)
    try:
        restarted = ExpressionEngine(reopened, engine(core).settings, core.clock)
        assert restarted.intent("evt_test_2", "真不错。⬡happy⬡").image is None
        assert restarted.intent("evt_test_2", "真不错。⬡like⬡").image is not None
    finally:
        reopened.close()
    for number in range(3, 10):
        queued(core, number, reply="普通文字。")
    assert engine(core).intent("evt_test_9", "真不错。⬡happy⬡").image is not None


def test_many_text_replies_do_not_evict_active_image_cooldown(core):
    queued(core)
    for number in range(2, 45):
        queued(core, number, reply="普通文字。")
    assert engine(core).intent("evt_test_44", "真不错。⬡like⬡").image is None


@pytest.mark.parametrize("sending", [False, True])
def test_expression_erasure_and_old_backup_replay_clear_text_and_media_intent(
    empty_database, core, tmp_path, sending
):
    row = queued(core)
    if sending:
        item = lease(core)[0]
        begin_send(core.store, item["message_id"], item["lease_token"], core.clock())
    key, archive = Fernet.generate_key(), tmp_path / "before.enc"
    restored_state = tmp_path / "restored-state"
    backup(core.settings.database, archive, key)
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    restore(archive, empty_database, key, core.settings.erasure_ledger,
            state_directory=restored_state)
    restored = Core(core.settings.model_copy(update={
        "database": empty_database, "state_directory": str(restored_state),
    }), clock=core.clock)
    try:
        for instance in (core, restored):
            stored = instance.store.one(
                'SELECT * FROM outbox WHERE message_id=%s', (row["message_id"],)
            )
            assert stored["content_json"] == "{}" and stored["content_sha256"] == ""
            assert stored["state"] == ("DELIVERY_UNCERTAIN" if sending else "CANCELLED")
            assert engine(instance).intent("evt_test_1", "文字。⬡happy⬡").image is None
    finally:
        restored.close()
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        engine(core).enqueue(
            "evt_test_1", row["conversation_id"], "文字", core.clock(), key="fresh"
        )


def test_plugin_library_verifies_real_bytes_dimensions_and_keeps_no_copy(tmp_path):
    source = picture(tmp_path)
    library = MemeLibrary(tmp_path, enabled=True)
    original = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    selected = library.prepare(image_intent(), "reply:first:v1")
    assert selected.data == source.read_bytes()
    assert (selected.width, selected.height, selected.mime_type) == (32, 24, "image/png")
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == original
    assert MemeLibrary(tmp_path).prepare(image_intent(), "key") is None


@pytest.mark.parametrize(
    "intent",
    [
        {"source": "https://example.com/image.png", "category": "happy"},
        {"source": "astrbot.meme_manager", "category": "../private"},
        {"source": "astrbot.meme_manager", "category": "happy", "path": "secret.png"},
        {"source": "astrbot.meme_manager", "category": ["happy"]},
    ],
)
def test_plugin_library_refuses_unapproved_origins_paths_and_shapes(tmp_path, intent):
    picture(tmp_path)
    assert MemeLibrary(tmp_path, enabled=True).prepare(intent, "key") is None


def test_plugin_library_rejects_corrupt_large_and_animated_images(tmp_path):
    source = picture(tmp_path)
    assert MemeLibrary(tmp_path, enabled=True, max_bytes=2).prepare(image_intent(), "key") is None
    assert (
        MemeLibrary(tmp_path, enabled=True, max_dimension=16).prepare(image_intent(), "key") is None
    )
    assert (
        MemeLibrary(tmp_path, enabled=True, max_total_pixels=32).prepare(image_intent(), "key")
        is None
    )
    source.write_bytes(b"not actually an image")
    assert MemeLibrary(tmp_path, enabled=True).prepare(image_intent(), "key") is None
    source.unlink()
    frame = Image.new("RGB", (16, 16), "green")
    frame.save(
        tmp_path / "happy/a.gif",
        save_all=True,
        append_images=[Image.new("RGB", (16, 16), "blue")],
        duration=100,
    )
    assert MemeLibrary(tmp_path, enabled=True, max_frames=1).prepare(image_intent(), "key") is None


def test_plugin_hash_dedup_catches_same_bytes_under_different_names_and_categories(tmp_path):
    source = picture(tmp_path)
    copy = tmp_path / "like/copy.png"
    copy.parent.mkdir()
    copy.write_bytes(source.read_bytes())
    (source.parent / "duplicate.png").write_bytes(source.read_bytes())
    clock = SimpleNamespace(now=0)
    library = MemeLibrary(tmp_path, enabled=True, clock=lambda: clock.now)
    first = library.prepare(image_intent(), "first")
    library.remember(first)
    assert library.prepare(image_intent(), "second") is None
    assert library.prepare(image_intent("like"), "third") is None
    clock.now = 1801
    assert library.prepare(image_intent("like"), "fourth").sha256 == first.sha256


def test_plugin_symlink_escape_is_rejected(tmp_path):
    outside = picture(tmp_path, "outside/private.png")
    root = tmp_path / "memes"
    (root / "happy").mkdir(parents=True)
    link = root / "happy/link.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink privileges unavailable")
    assert MemeLibrary(root, enabled=True).prepare(image_intent(), "key") is None


class CoreAPI:
    """Exercise the same state transitions while keeping this transport test local."""

    def __init__(self, core, *, begin_timeout=False, result_timeout=False):
        self.core = core
        self.begin_timeout = begin_timeout
        self.result_timeout = result_timeout
        self.calls = []

    async def post(self, path, json):
        self.calls.append(path)
        message_id = path.split("/")[-2]
        if path.endswith("begin-send"):
            result = begin_send(self.core.store, message_id, json["lease_token"], self.core.clock())
            if self.begin_timeout:
                raise httpx.ReadTimeout("unknown commit")
        else:
            if self.result_timeout:
                raise httpx.ReadTimeout("unknown ack")
            result = delivery_result(
                self.core.store,
                self.core.settings,
                message_id,
                DeliveryResult.model_validate(json),
                self.core.clock(),
            )
        return httpx.Response(200, json=result, request=httpx.Request("POST", "http://core" + path))


@pytest.mark.parametrize("failure", [None, "missing", "builder", "send_false", "send_error"])
def test_expression_single_chain_send_and_preflight_fallback(core, tmp_path, failure):
    queued(core)
    item = lease(core)[0]
    if failure != "missing":
        picture(tmp_path)
    library = MemeLibrary(tmp_path, enabled=True)
    sent = []

    def build(text, data):
        if failure == "builder" and data:
            raise ValueError("unsupported image constructor")
        return {"text": text, "image": data}

    async def send(origin, chain):
        sent.append((origin, chain))
        if failure == "send_error":
            raise httpx.ReadTimeout("ambiguous platform result")
        return None if failure == "send_false" else "42"

    report = run(deliver_expression(CoreAPI(core), item, library, build, send,
                                    spool=journal(tmp_path)))
    assert len(sent) == 1 and sent[0][1]["text"] == "真不错。"
    assert (sent[0][1]["image"] is None) == (failure in {"missing", "builder"})
    assert report["result"] == (
        "DELIVERY_UNCERTAIN" if failure in {"send_false", "send_error"} else "SENT"
    )
    if failure in {"missing", "builder"}:
        assert report["error_code"] == "IMAGE_PREFLIGHT_SKIPPED"
    with pytest.raises(CoreError):
        run(deliver_expression(CoreAPI(core), item, library, build, send, spool=journal(tmp_path)))
    assert len(sent) == 1
    assert lease(core) == []


@pytest.mark.parametrize("timeout", ["begin", "result"])
def test_expression_ack_timeout_never_replays_text_or_media(core, tmp_path, timeout):
    queued(core)
    item = lease(core)[0]
    picture(tmp_path)
    sent = []

    async def send(origin, chain):
        sent.append(chain)
        return "42"

    api = CoreAPI(core, begin_timeout=timeout == "begin", result_timeout=timeout == "result")
    with pytest.raises(httpx.ReadTimeout):
        run(
            deliver_expression(
                api,
                item,
                MemeLibrary(tmp_path, enabled=True),
                lambda text, image: (text, image),
                send, spool=journal(tmp_path),
            )
        )
    assert len(sent) == (0 if timeout == "begin" else 1)
    core.clock.advance(121)
    assert lease(core) == []
    assert core.store.one("SELECT state FROM outbox")["state"] == "DELIVERY_UNCERTAIN"


def test_erasure_after_image_preparation_prevents_any_platform_send(core, tmp_path):
    queued(core)
    item = lease(core)[0]
    picture(tmp_path)
    sent = []

    def build(text, image):
        with core.store.transaction(control_write=True):
            core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
        return text, image

    async def send(origin, chain):
        sent.append(chain)
        return "42"

    with pytest.raises(CoreError, match="STALE_DELIVERY_LEASE"):
        run(
            deliver_expression(
                CoreAPI(core), item, MemeLibrary(tmp_path, enabled=True), build, send,
                spool=journal(tmp_path)
            )
        )
    assert sent == []
    assert core.store.one("SELECT content_json,state FROM outbox") == {
        "content_json": "{}",
        "state": "CANCELLED",
    }


def test_core_chat_creates_expression_intent_in_normal_outbox(core):
    from companion.model import ModelResponse

    core.settings.expression_enabled = True

    async def complete(messages, scenario):
        assert "⬡类别⬡" in messages[0]["content"]
        return ModelResponse("通关了，真不错！⬡happy⬡", 20, 10, 0)

    core.model.complete = complete
    ingest(core, text="今天把游戏打通关了")
    assert run(core.run_once("expression-integration"))
    content = json.loads(core.store.one("SELECT content_json FROM outbox")["content_json"])
    assert content == {
        "text": "通关了，真不错！",
        "image": {"source": "astrbot.meme_manager", "category": "happy"},
    }


def test_expression_categories_from_environment_validate(core, monkeypatch, tmp_path):
    from pydantic import ValidationError

    from companion.config import Settings

    monkeypatch.setenv("COMPANION_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("COMPANION_DATABASE", core.settings.database.get_secret_value())
    monkeypatch.setenv("COMPANION_STATE_DIRECTORY", core.settings.state_directory)
    monkeypatch.setenv("COMPANION_OWNER_QQ_ID", "10001")
    monkeypatch.setenv("COMPANION_BOT_QQ_ID", "10002")
    monkeypatch.setenv("COMPANION_BRIDGE_TOKEN", "config-test-token-" * 3)
    monkeypatch.setenv("COMPANION_EXPRESSION_MEME_CATEGORIES", '["happy","like"]')
    assert Settings.load().expression_meme_categories == ["happy", "like"]
    monkeypatch.setenv("COMPANION_EXPRESSION_MEME_CATEGORIES", '["../../private"]')
    with pytest.raises(ValidationError):
        Settings.load()

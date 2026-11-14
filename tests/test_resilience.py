import importlib
import json
import sys
import types

import pytest
from fastapi.testclient import TestClient

from companion.contracts import CoreError
from companion.core import Core
from companion.store import migrate
from companion.transport import begin_send, reconcile_delivery
from companion.worker_lock import single_worker
from tests.conftest import ingest, run
from tests.support.basic_chat import lease
from tests.support.confirmed_memory import remember


def test_retired_astrbot_handler_stops_without_new_spooling(tmp_path, monkeypatch):
    modules = {
        name: types.ModuleType(name)
        for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star")
    }
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = types.SimpleNamespace(error=lambda *a: None)
    api = modules["astrbot.api.event"]
    api.AstrMessageEvent = object
    api.MessageChain = object
    api.filter = types.SimpleNamespace(
        EventMessageType=types.SimpleNamespace(PRIVATE_MESSAGE=1),
        event_message_type=lambda *a, **k: lambda method: method,
    )
    star = modules["astrbot.api.star"]

    class Star:
        def __init__(self, context):
            self.context = context

    star.Star = Star
    star.Context = object
    star.StarTools = object
    star.register = lambda *a: lambda cls: cls
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    module = importlib.import_module("astrbot_plugin_companion_bridge.main")
    config = {"owner_qq_id": "10001", "bot_qq_id": "10002", "bridge_token": "x" * 32}
    bridge = module.CompanionBridge(object(), config)
    order = []
    bridge.spool = types.SimpleNamespace(put=lambda *args: order.append("spool"))
    ev = types.SimpleNamespace(
        get_platform_name=lambda: "aiocqhttp",
        get_sender_id=lambda: "10001",
        get_group_id=lambda: "",
        unified_msg_origin="qq:FriendMessage:10001",
        message_str="hi",
        stop_event=lambda: order.append("stop"),
        message_obj=types.SimpleNamespace(self_id="10002", message_id="42", timestamp=1792382400),
    )
    run(bridge.on_private_message(ev))
    assert order == ["stop"]
    order.clear()
    ev.get_sender_id = lambda: "99999"
    run(bridge.on_private_message(ev))
    assert order == []
    sys.modules.pop("astrbot_plugin_companion_bridge.main", None)


def test_uncertainty_does_not_block_next_reply_or_resend(core):
    ingest(core, 1)
    run(core.run_once())
    first = lease(core)[0]
    begin_send(core.store, first["message_id"], first["lease_token"], core.clock())
    core.clock.advance(121)
    ingest(core, 2)
    run(core.run_once())
    second = lease(core)[0]
    assert second["message_id"] != first["message_id"]
    reconcile_delivery(core.store, first["message_id"], "CANCELLED", core.clock())
    with pytest.raises(CoreError):
        reconcile_delivery(core.store, second["message_id"], "SENT", core.clock())


def test_unknown_price_blocks_autonomy_only(core):
    core.settings.input_rate = None
    core.settings.proactive_enabled = True
    core.settings.social_enabled = True
    ingest(core)
    run(core.run_once())
    assert core.store.one("SELECT state FROM jobs")["state"] == "SUCCEEDED"
    assert not core.social.flush()
    assert core.status()["usage"][0]["unknown_price_requests"] == 1


def test_monthly_currency_amount_does_not_refuse_a_valid_reply(core):
    ingest(core)
    run(core.run_once())
    assert len(core.model.calls) == 1
    assert (
        core.store.one("SELECT last_error_code FROM jobs")["last_error_code"]
        is None
    )


def test_zero_extraction_currency_amount_does_not_disable_processing(core):
    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 1
    core.settings.extraction_idle_seconds = 0
    ingest(core)
    run(core.run_once())
    assert run(core.extract_once())
    assert core.store.one("SELECT memory_processed FROM events")["memory_processed"] == 1


def test_erasure_ledger_loss_blocks_startup(core):
    ledger = core.ledger.path
    retained = ledger.with_suffix(".saved")
    ledger.rename(retained)
    with pytest.raises(CoreError, match="ERASURE_LEDGER_MISSING"):
        Core(core.settings)
    retained.rename(ledger)


def test_raw_retention_is_replayed_and_downgrades_memory(core):
    remember(core, 1, "我喜欢茉莉绿茶", confirmed=True)
    core.clock.advance(366 * 86400)
    assert core.expire_raw_events() == 1
    assert core.memory.search("茉莉") == []
    assert core.store.one("SELECT body_json FROM events")["body_json"] == "{}"


def test_single_worker_lock_releases(core):
    with single_worker(core.settings.state_directory):
        with pytest.raises(CoreError, match="WORKER_ALREADY_RUNNING"):
            with single_worker(core.settings.state_directory):
                pass
    with single_worker(core.settings.state_directory):
        pass


def test_oversized_body_and_validation_do_not_echo_secrets(core):
    from companion.api import create_app

    headers = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
    with TestClient(create_app(core.settings)) as client:
        result = client.post("/internal/v1/inbox/events", content=b"x" * 140000, headers=headers)
        assert result.status_code == 413
        result = client.post(
            "/internal/v1/inbox/events", json={"secret": "DO_NOT_ECHO"}, headers=headers
        )
        assert result.status_code == 400 and "DO_NOT_ECHO" not in result.text


def test_correction_removes_superseded_fact_from_context(core):
    memory_id = remember(core, 1, "我喜欢绿茶", confirmed=True)
    core.memory.decide(memory_id, "correct", core.clock(), "我喜欢红茶")
    ingest(core, 2, "我们聊聊饮料")
    job = core.store.one(
        "SELECT j.*,e.body_json,e.conversation_id FROM jobs j JOIN events e "
        "ON e.event_id=j.event_id WHERE j.event_id='evt_test_2'"
    )
    assert "我喜欢绿茶" not in json.dumps(core.context(job), ensure_ascii=False)


def test_native_migrations_require_restore_for_downgrade(empty_database):
    from pathlib import Path

    import psycopg
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    migrate(empty_database, "pg0001_core")
    cfg = Config()
    cfg.set_main_option("script_location", str(Path("migrations/postgresql").resolve()))
    engine = create_engine(
        "postgresql+psycopg://", poolclass=NullPool, hide_parameters=True,
        creator=lambda: psycopg.connect(empty_database.get_secret_value()),
    )
    try:
        with pytest.raises(RuntimeError, match="CORE_POSTGRES_DOWNGRADE_REQUIRES_BACKUP_RESTORE"):
            with engine.begin() as connection:
                cfg.attributes["connection"] = connection
                command.downgrade(cfg, "base")
        migrate(empty_database)
        with psycopg.connect(empty_database.get_secret_value()) as db:
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "pg0008_companion_avatar",)
    finally:
        engine.dispose()

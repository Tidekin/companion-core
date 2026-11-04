"""Separate controls use actual authenticated loopback WS/HTTP, never QQ or paid APIs."""

import asyncio
import copy
import hashlib
import socket
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr, ValidationError
from websockets.asyncio.server import serve

from companion import budget, live_text, paid_budget
from companion.api import create_app
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.memory_control import (
    RESTORED,
    SIGNATURE_DOMAIN,
    MemoryControlGrant,
    StoppedMemoryControl,
    load_grant,
    stopped_state,
    stopped_workers,
    target_digest,
)
from companion.privacy import advance_epoch, epoch
from companion.store import Store, encode, uid
from companion.worker_lock import single_worker
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.memory_control import capture_once, execute_once
from companion_onebot_bridge.rich_input import input_digest
from companion_onebot_bridge.window import bridge_code_sha256
from tests.conftest import ingest, run
from tests.support.native_http import owned_http_protocol
from tests.support.onebot import onebot_http
from tests.support.onebot_events import config, raw_event


def bridge(core, **changes):
    core.settings.control_token = SecretStr("synthetic-control-token-32-characters")
    return config(capture_signing_key=Ed25519PrivateKey.generate().private_bytes_raw().hex(),
                  onebot_http_url="http://127.0.0.1:3000",
                  onebot_ws_url="ws://127.0.0.1:3001", **changes)


def grant(core, cfg, text, *, operation="remember", target=None, **changes):
    restored = core.store.one("SELECT value FROM runtime_meta WHERE key=?", (RESTORED,))
    values = dict(
        execution="local_synthetic", grant_id=uid("mc"), operation=operation,
        permission={"remember": "memory.write", "confirm": "memory.write",
                    "correct": "memory.correct", "forget": "memory.forget"}[operation],
        max_commands=1, starts_at=core.clock.now - timedelta(seconds=1),
        ends_at=core.clock.now + timedelta(minutes=10), privacy_epoch=epoch(core.store),
        restore_generation=restored["value"] if restored else None,
        owner_approval_sha256="a" * 64, stopped_services_sha256="b" * 64,
        ledger_sha256=hashlib.sha256(Path(core.settings.erasure_ledger).read_bytes()).hexdigest(),
        stopped_state_sha256=stopped_state(core.store),
        identity_sha256=paid_budget.sha([core.settings.owner_qq_id, core.settings.bot_qq_id]),
        core_config_sha256=live_text.config_sha256(core.settings),
        core_code_sha256=live_text.code_sha256(),
        bridge_config_sha256=paid_budget.sha(cfg.snapshot()),
        bridge_code_sha256=bridge_code_sha256(), capture_public_key=cfg.capture_public_key,
        protocol_epoch=cfg.protocol_epoch, command_sha256=input_digest({"text": text}),
        target_memory_id=target,
        target_sha256=target_digest(core.store, target) if target else None,
        fact_key="synthetic_project_name", subject="project",
    )
    return MemoryControlGrant(**(values | changes))


def service(core, approval):
    return StoppedMemoryControl(core.store, core.settings, core.ledger, core.clock, approval,
                                paid_budget.sha(approval.model_dump(mode="json")))


def captured(core, cfg, approval, text, number=1):
    frame = encode(raw_event(number, text=text, time=int(core.clock.now.timestamp())))
    event = normalize(frame, cfg, now=core.clock.now.timestamp(), rich_input_scope="none")
    receipt = {
        "kind": "authenticated_onebot_ws",
        "window_sha256": paid_budget.sha(approval.model_dump(mode="json")),
        "event_id": event["event_id"], "source_event_id": event["source_event_id"],
        "input_sha256": input_digest(event["payload"]),
        "frame_sha256": hashlib.sha256(frame.encode()).hexdigest(),
        "evidence_sha256": "e" * 64, "identity_sha256": approval.identity_sha256,
        "config_sha256": approval.bridge_config_sha256, "protocol_epoch": cfg.protocol_epoch,
        "received_at": core.clock.now.isoformat(), "occurred_at": event["occurred_at"],
    }
    receipt["signature"] = Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(cfg.capture_signing_key)
    ).sign(SIGNATURE_DOMAIN + encode(receipt).encode()).hex()
    return {"event": event, "capture": receipt}


def remember(core, cfg, number=1, text="项目的代号是青竹七号。", **options):
    command = "/记住这个：" + text
    approval = grant(core, cfg, command, **options)
    result = service(core, approval).apply(captured(core, cfg, approval, command, number))
    return result["memory_id"]


@asynccontextmanager
async def real_http(app, listener=None):
    ready = asyncio.Event()

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    if listener is None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
    server = Server(uvicorn.Config(app, lifespan="off", log_config=None, access_log=False,
                                   http=owned_http_protocol(lambda: server.servers)))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        yield listener.getsockname()[1]
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()


def test_real_authenticated_ws_to_original_http_api_writes_once_without_chat(core, tmp_path):
    text = "/记住这个：项目的代号是青竹七号。"
    cfg = bridge(core, data_dir=str(tmp_path / "spool"))
    handshakes, sends = [], []

    async def scenario():
        async def source(ws):
            await ws.send(encode(raw_event(1, text=text, time=int(core.clock.now.timestamp()))))

        async def authenticate(connection, request):
            handshakes.append(request.headers.get("Authorization"))
            if request.headers.get("Authorization") != "Bearer " + cfg.onebot_token:
                return connection.respond(401, "synthetic auth required")

        with onebot_http(sends, cfg.onebot_token) as http_port:
            async with serve(source, "127.0.0.1", 0, process_request=authenticate) as ws:
                listener = socket.socket()
                listener.bind(("127.0.0.1", 0))
                pinned = replace(cfg, onebot_http_url=f"http://127.0.0.1:{http_port}",
                                 core_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
                                 onebot_ws_url=f"ws://127.0.0.1:{ws.sockets[0].getsockname()[1]}")
                approval = grant(core, pinned, text)
                approved_sha = paid_budget.sha(approval.model_dump(mode="json"))
                app = create_app(core.settings, memory_control_grant=approval,
                                 memory_control_sha256=approved_sha)
                app.state.core = core
                async with real_http(app, listener) as port:
                    initial = await execute_once(pinned, approval.model_dump(mode="json"),
                        approved_sha, core.settings.control_token.get_secret_value(),
                        clock=lambda: core.clock.now)
                    assert initial["state"] == "CONFIRMED" and not initial["duplicate"]
                    body = await capture_once(pinned, approval.model_dump(mode="json"),
                                              approved_sha, clock=lambda: core.clock.now)
                    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                                                 trust_env=False) as client:
                        wrong = await client.post("/internal/v1/memory-control", json=body,
                                                  headers={"Authorization": "Bearer wrong"})
                        assert wrong.status_code == 401
                        headers = {"Authorization": "Bearer " +
                                   core.settings.control_token.get_secret_value()}
                        first = await client.post("/internal/v1/memory-control", json=body,
                                                  headers=headers)
                        assert first.status_code == 200, first.text
                        assert first.json() == initial | {"duplicate": True}
                        duplicate = await client.post("/internal/v1/memory-control", json=body,
                                                      headers=headers)
                        assert duplicate.json() == first.json() | {"duplicate": True}
                        blocked = await client.post("/internal/v1/inbox/events", json=body["event"],
                            headers={"Authorization": "Bearer " +
                                     core.settings.bridge_token.get_secret_value()})
                        assert blocked.json() == {"error": "MEMORY_CONTROL_EXCLUSIVE"}

    run(scenario())
    assert handshakes == ["Bearer " + cfg.onebot_token] * 2
    assert sends == core.model.calls == []
    assert core.store.one("SELECT COUNT(*) n FROM memories")["n"] == 1
    assert core.store.one(
        "SELECT COUNT(*) n FROM audit_log WHERE action='memory.control'")["n"] == 1
    assert not core.settings.memory_enabled
    for table in ("jobs", "outbox", "model_usage", "model_admissions"):
        assert not core.store.rows(f"SELECT * FROM {table}")


def test_default_api_has_no_memory_control_authority(core):
    cfg = bridge(core)
    text = "/记住这个：项目的代号是青竹七号。"
    approval = grant(core, cfg, text)
    app = create_app(core.settings)
    app.state.core = core

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.post("/internal/v1/memory-control",
                json=captured(core, cfg, approval, text), headers={"Authorization": "Bearer " +
                     core.settings.control_token.get_secret_value()})
            assert response.json() == {"error": "MEMORY_CONTROL_SCOPE_REQUIRED"}

    run(scenario())
    assert not core.store.rows("SELECT * FROM events")


@pytest.mark.parametrize("mutation", ["actor", "signature", "text", "frame", "epoch",
                                     "event_id", "source_id", "rich", "media", "timestamp"])
def test_source_and_capture_tampering_are_rejected_before_any_write(core, mutation):
    cfg = bridge(core)
    text = "/记住这个：项目的代号是青竹七号。"
    approval = grant(core, cfg, text)
    body = captured(core, cfg, approval, text)
    if mutation == "actor":
        body["event"]["actor"]["platform_user_id"] = "10009"
    elif mutation in {"signature", "frame"}:
        body["capture"]["signature" if mutation == "signature" else "frame_sha256"] = (
            "f" * (128 if mutation == "signature" else 64))
    elif mutation == "text":
        body["event"]["payload"]["text"] += "换成红色"
    elif mutation == "epoch":
        body["capture"]["protocol_epoch"] = "another-epoch"
    elif mutation == "event_id":
        body["event"]["event_id"] = "evt_" + "f" * 64
    elif mutation == "source_id":
        body["event"]["source_event_id"] += "0"
    elif mutation == "rich":
        body["event"]["payload"]["rich"] = {"version": "references_v1", "coverage": "PARTIAL",
            "segments": [{"index": 0, "type": "reply", "actor": "UNVERIFIED",
                          "coverage": "MISSING", "reference": "1"}],
            "segment_count": 2, "owner_text_positions": [1]}
    elif mutation == "media":
        body["event"]["payload"]["media"] = [
            {"kind": "image", "media_ref": "synthetic", "sha256": "a" * 64}]
    else:
        core.clock.advance(31)
    with pytest.raises(CoreError):
        service(core, approval).apply(body)
    assert not core.store.rows("SELECT * FROM events")
    assert not core.store.rows("SELECT * FROM memories")


@pytest.mark.parametrize("message", [
    "／记住这个：普通内容", "> /记住这个：引用", " /记住这个：前空格", "记住这个：普通谈话",
    [{"type": "reply", "data": {"id": "1"}},
     {"type": "text", "data": {"text": "/记住这个：来自引用"}}],
    [{"type": "forward", "data": {"id": "abc"}}],
    [{"type": "json", "data": {"data": '{"title":"/记住这个：来自卡片"}'}}],
    [{"type": "image", "data": {"file": "synthetic"}}],
    "[CQ:reply,id=1]/记住这个：伪标签",
])
def test_receiver_never_promotes_rich_or_similar_text_to_control(core, message):
    cfg = bridge(core)

    async def scenario():
        async def source(ws):
            await ws.send(encode(raw_event(1, message=message,
                                            time=int(core.clock.now.timestamp()))))

        with onebot_http([], cfg.onebot_token) as http_port:
            async with serve(source, "127.0.0.1", 0) as ws:
                pinned = replace(cfg, onebot_http_url=f"http://127.0.0.1:{http_port}",
                                 onebot_ws_url=f"ws://127.0.0.1:{ws.sockets[0].getsockname()[1]}")
                approval = grant(core, pinned, "/记住这个：当前主人正文")
                with pytest.raises(ValueError):
                    await capture_once(pinned, approval.model_dump(mode="json"),
                        paid_budget.sha(approval.model_dump(mode="json")),
                        clock=lambda: core.clock.now)

    run(scenario())
    assert not core.store.rows("SELECT * FROM events")


@pytest.mark.parametrize("change", ["expiry", "privacy", "owner", "ledger", "writer", "target"])
def test_current_scope_is_revalidated_at_commit(core, change):
    cfg = bridge(core)
    mid = remember(core, cfg)
    text = f"/纠正这一条 {mid} 项目的代号是白帆九号。"
    approval = grant(core, cfg, text, operation="correct", target=mid)
    body = captured(core, cfg, approval, text, 2)
    if change == "expiry":
        core.clock.advance(601)
    elif change == "privacy":
        advance_epoch(core.store)
    elif change == "owner":
        core.settings.owner_qq_id = "10009"
    elif change == "ledger":
        with Path(core.settings.erasure_ledger).open("a") as stream:
            stream.write("\n")
    elif change == "writer":
        core.settings.diary_enabled = True
    else:
        core.store.execute("UPDATE memories SET content='来源发生变化' WHERE memory_id=?", (mid,))
    with pytest.raises(CoreError):
        service(core, approval).apply(body)
    assert not core.store.rows("SELECT * FROM memory_revisions")
    assert core.store.one("SELECT COUNT(*) n FROM events")["n"] == 1


def test_exact_scope_does_not_modify_existing_or_unrelated_memories(core):
    cfg = bridge(core)
    mid = remember(core, cfg)
    text = "/记住这个：项目的代号是青竹七号。"
    approval = grant(core, cfg, text)
    before = epoch(core.store)
    with pytest.raises(CoreError, match="EXPLICIT_EXISTING_TARGET_REQUIRED"):
        service(core, approval).apply(captured(core, cfg, approval, text, 2))
    assert epoch(core.store) == before
    assert core.store.one("SELECT COUNT(*) n FROM memory_evidence")["n"] == 1
    text = f"/忘记这一条 {mid}"
    approval = grant(core, cfg, text, operation="forget", target=mid)
    with pytest.raises(ValidationError):
        MemoryControlGrant.model_validate(approval.model_dump() | {"permission": "memory.write"})


def test_sensitive_fact_requires_a_separate_explicit_scope(core):
    cfg = bridge(core)
    text = "/记住这个：我的宗教偏好暂未确定。"
    approval = grant(core, cfg, text)
    with pytest.raises(CoreError, match="SENSITIVE_CONFIRMATION_REQUIRED"):
        service(core, approval).apply(captured(core, cfg, approval, text))
    authorized = grant(core, cfg, text, sensitive_confirmed=True,
                       sensitive_confirmation_sha256="c" * 64)
    result = service(core, authorized).apply(captured(core, cfg, authorized, text))
    assert result["state"] == "CONFIRMED"


def test_restart_replay_is_idempotent_and_later_privacy_change_does_not_report_stale_success(core):
    cfg = bridge(core)
    text = "/记住这个：项目的代号是青竹七号。"
    approval = grant(core, cfg, text)
    body = captured(core, cfg, approval, text)
    first = service(core, approval).apply(body)
    store = Store(core.settings.database)
    try:
        restarted = StoppedMemoryControl(store, core.settings, core.ledger, core.clock,
            approval, paid_budget.sha(approval.model_dump(mode="json")))
        assert restarted.apply(body) == first | {"duplicate": True}
        core.clock.advance(1)
        reconnected = copy.deepcopy(body)
        reconnected["capture"]["received_at"] = core.clock.now.isoformat()
        unsigned = {k: v for k, v in reconnected["capture"].items() if k != "signature"}
        reconnected["capture"]["signature"] = Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(cfg.capture_signing_key)
        ).sign(SIGNATURE_DOMAIN + encode(unsigned).encode()).hex()
        assert restarted.apply(reconnected) == first | {"duplicate": True}
        with pytest.raises(CoreError, match="ALREADY_CONSUMED"):
            restarted.apply(captured(core, cfg, approval, text, 2))
        different = copy.deepcopy(body)
        different["capture"]["evidence_sha256"] = "d" * 64
        with pytest.raises(CoreError):
            restarted.apply(different)
        advance_epoch(store)
        with pytest.raises(CoreError, match="RESULT_STALE"):
            restarted.apply(body)
    finally:
        store.close()
    assert core.store.one("SELECT COUNT(*) n FROM memories")["n"] == 1


def test_old_backup_cannot_reuse_an_unconsumed_or_consumed_control_grant(core, tmp_path):
    cfg = bridge(core)
    text = "/记住这个：项目的代号是青竹七号。"
    approval = grant(core, cfg, text)
    body = captured(core, cfg, approval, text)
    key = Fernet.generate_key()
    archive = tmp_path / "before-control.enc"
    backup(core.settings.database, archive, key)
    service(core, approval).apply(body)
    restored = tmp_path / "restored.db"
    restore(archive, restored, key, core.settings.erasure_ledger)
    store = Store(str(restored))
    try:
        # Keep exactly the original configuration to isolate the restore-generation barrier.
        replay = StoppedMemoryControl(store, core.settings, core.ledger, core.clock,
                                     approval, paid_budget.sha(approval.model_dump(mode="json")))
        with pytest.raises(CoreError, match="RESTORED_REAPPROVAL_REQUIRED"):
            replay.apply(body)
        assert not store.rows("SELECT * FROM memories")
        assert store.one("SELECT value FROM runtime_meta WHERE key=?", (RESTORED,))
    finally:
        store.close()


def test_forget_ledger_survives_commit_failure_and_retry_cannot_claim_success(core, monkeypatch):
    cfg = bridge(core)
    mid = remember(core, cfg)
    text = f"/忘记这一条 {mid}"
    approval = grant(core, cfg, text, operation="forget", target=mid)
    body = captured(core, cfg, approval, text, 2)
    original = core.store.transaction

    @contextmanager
    def fail_commit():
        with original():
            yield
            raise OSError("synthetic commit failure after fsynced Ledger append")

    with monkeypatch.context() as scoped:
        scoped.setattr(core.store, "transaction", fail_commit)
        with pytest.raises(OSError):
            service(core, approval).apply(body)
    assert len(core.ledger.read()) == 2
    assert core.store.one("SELECT status FROM memories WHERE memory_id=?", (mid,)) == {
        "status": "CONFIRMED"}
    with pytest.raises(CoreError, match="ERASURE_REPLAY_REQUIRED"):
        service(core, approval).apply(body)
    core.ledger.replay(core.store)
    assert core.store.one("SELECT content FROM memories WHERE memory_id=?", (mid,))["content"] == ""
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        service(core, approval).apply(body)
    assert len(core.ledger.read()) == 2
    assert core.store.one(
        "SELECT COUNT(*) n FROM audit_log WHERE action='memory.control'")["n"] == 1


@pytest.mark.parametrize("kind", ["worker", "diary-worker", "life-worker",
                                 "executive-worker", "content-worker"])
def test_control_process_cannot_coexist_with_any_business_worker(core, kind):
    with stopped_workers(core.settings.database), pytest.raises(CoreError, match="ALREADY_RUNNING"):
        with single_worker(core.settings.database, kind):
            pytest.fail("worker acquired the control process's lock")


def test_real_scope_requires_business_acceptance_and_precise_stopped_window(core):
    cfg = bridge(core)
    approval = grant(core, cfg, "/记住这个：项目的代号是青竹七号。")
    with pytest.raises(ValidationError, match="BUSINESS_R5"):
        MemoryControlGrant.model_validate(approval.model_dump() | {
            "execution": "approved_stopped_r5"})


def test_approval_file_is_the_same_canonical_scope_for_core_and_bridge(core, tmp_path):
    cfg = bridge(core)
    approval = grant(core, cfg, "/记住这个：项目的代号是青竹七号。")
    value = approval.model_dump(mode="json")
    approved_sha = paid_budget.sha(value)
    path = tmp_path / "approved.json"
    path.write_text(encode(value), "utf-8")
    path.chmod(0o600)
    assert load_grant(path, approved_sha) == approval
    with pytest.raises(CoreError, match="APPROVAL_MISMATCH"):
        load_grant(path, "f" * 64)
    del value["sensitive_confirmation_sha256"]
    path.write_text(encode(value), "utf-8")
    with pytest.raises(CoreError, match="CANONICAL_APPROVAL_REQUIRED"):
        load_grant(path, approved_sha)
    path.write_text('{"execution":"local_synthetic","execution":"approved_stopped_r5"}', "utf-8")
    with pytest.raises(ValueError, match="DUPLICATE_FIELD"):
        load_grant(path, approved_sha)


def test_confirm_control_and_foreign_signature_domain(core):
    cfg = bridge(core)
    mid = remember(core, cfg)
    core.store.execute("UPDATE memories SET status='HYPOTHESIS',confidence=0.5 WHERE memory_id=?",
                       (mid,))
    text = f"/确认这一条 {mid}"
    approval = grant(core, cfg, text, operation="confirm", target=mid)
    body = captured(core, cfg, approval, text, 2)
    foreign = copy.deepcopy(body)
    unsigned = {key: value for key, value in foreign["capture"].items() if key != "signature"}
    foreign["capture"]["signature"] = Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(cfg.capture_signing_key)
    ).sign(live_text.CAPTURE_SIGNATURE_DOMAIN + encode(unsigned).encode()).hex()
    with pytest.raises(CoreError, match="CAPTURE_INVALID"):
        service(core, approval).apply(foreign)
    assert service(core, approval).apply(body)["state"] == "CONFIRMED"
    assert core.store.one("SELECT COUNT(*) n FROM memory_evidence")["n"] == 1


def test_concurrent_forget_scopes_append_only_one_erasure(core):
    cfg = bridge(core)
    mid = remember(core, cfg)
    text = f"/忘记这一条 {mid}"
    approvals = [grant(core, cfg, text, operation="forget", target=mid) for _ in range(2)]

    def execute(item):
        number, approval = item
        store = Store(core.settings.database)
        try:
            control = StoppedMemoryControl(store, core.settings, core.ledger, core.clock,
                approval, paid_budget.sha(approval.model_dump(mode="json")))
            try:
                return control.apply(captured(core, cfg, approval, text, number + 2))["state"]
            except CoreError as error:
                return error.code
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, enumerate(approvals)))
    assert sorted(results) == ["FORGOTTEN", "PRIVACY_CHANGED"]
    assert len(core.ledger.read()) == 2
    assert not core.store.rows("SELECT * FROM memory_revisions")


@pytest.mark.parametrize("pending", ["QUEUED", "RESERVED", "UNKNOWN"])
def test_control_refuses_inflight_chat_or_cost_without_resetting_it(core, pending):
    cfg = bridge(core)
    text = "/记住这个：项目的代号是青竹七号。"
    approval = grant(core, cfg, text)
    body = captured(core, cfg, approval, text)
    if pending == "QUEUED":
        ingest(core, 99, "正在等待聊天")
    else:
        budget.reserve(core.store, core.settings, None, "chat",
                       [{"role": "user", "content": "合成尚未结算的请求"}], core.clock())
        core.store.execute("UPDATE model_usage SET state=?", (pending,))
    usage, jobs = (core.store.rows("SELECT * FROM " + table) for table in ("model_usage", "jobs"))
    with pytest.raises(CoreError, match="INITIAL_WORK_MUST_BE_ZERO"):
        service(core, approval).apply(body)
    assert core.store.rows("SELECT * FROM model_usage") == usage
    assert core.store.rows("SELECT * FROM jobs") == jobs
    assert not core.store.rows("SELECT * FROM memories")
    assert core.model.calls == []

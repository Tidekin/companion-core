"""Actual account observations cross the explicit read-only Core grant boundary."""

import asyncio
import hashlib
import io
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from PIL import Image

import companion_delivery_protocol
from companion.core import Core
from companion.model import ModelClient
from companion.retrieval import SemanticIndex
from companion_onebot_bridge import profile_probe
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.owner_memes import MemePump
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from companion_onebot_bridge.window import bridge_code_sha256, digest
from tests.conftest import run
from tests.test_native_account_host import configure_host
from tests.test_onebot_account_actions import fixture
from tests.test_onebot_capture import real_core_http
from tests.test_onebot_direct import raw_event
from tests.test_onebot_pokes import silent
from tests.test_social_owner_authority import authorize, close_provider_clients  # noqa: F401


@asynccontextmanager
async def source_window(core, tmp_path, source, *, live, approved, scope_options,
                        protocol_reply, bridge_options=None, account_scope=True):
    from dataclasses import replace

    from companion.api import create_app
    from tests.test_onebot_capture import provider

    core.settings.social_autonomy = scope_options["core_options"]["social_autonomy"]
    policy, _ = authorize(core, tmp_path, account=account_scope)
    cfg = configure_host(core, tmp_path, policy)
    if bridge_options:
        cfg = replace(cfg, **bridge_options)
    app = create_app(core.settings)
    app.state.core = core

    def native(request):
        return protocol_reply(request) or provider(request, [])

    with Spool(cfg) as spool:
        runtime = OwnerRuntime(spool, clock=lambda: datetime.fromisoformat(core.clock()))
        async with private_http_client(cfg.core_url, cfg.bridge_token,
            transport=httpx.ASGITransport(app)) as client, private_http_client(
                cfg.onebot_http_url, cfg.onebot_token,
                transport=httpx.MockTransport(native)) as protocol:
            yield runtime, client, protocol, [], []


def options(core, fields=("nickname", "signature", "avatar")):
    _, _, scope, _ = fixture(core)
    policy = scope["core_options"]["social_autonomy"]
    scope["core_options"]["social_autonomy"] = policy.model_copy(update={
        field + "_enabled": field in fields for field in profile_probe.FIELDS})
    return scope


def profile(**changes):
    return {"status": "ok", "retcode": 0, "data": {"user_id": 10002,
        "uid": "u_" + "a" * 22, "nickname": "当前名字", "long_nick": "当前签名", **changes}}


def full_profile(*, optional=True, **changes):
    # SnowLuma v2.0.3 formatStrangerInfo: 21 required keys plus four optional keys.
    fields = {"uid": "", "qid": "synthetic-qid", "remark": "", "sex": "unknown", "age": 0,
        "qq_level": 12, "qqLevel": 12, "level": 12, "status": 10, "extStatus": 0,
        "ext_status": 0, "batteryStatus": 0, "customStatus": None, "customStatusDescInfo": "",
        "qidian_master_flag": 0, "qidian_crew_flag": 0, "qidian_crew_flag_2": 0,
        "qidian_enterprise_name": ""}
    if optional:
        fields.update(reg_time=1_200_000_000, is_vip=False, is_years_vip=False, vip_level=0)
    return profile(**(fields | changes))


@pytest.mark.parametrize("variant", ["complete", "required_only", "empty_signature"])
def test_empty_uid_complete_native_profile_preserves_explicit_field_observations(
    core, tmp_path, variant,
):
    scope = options(core, ("nickname", "signature"))
    response = full_profile(optional=variant != "required_only",
        long_nick="" if variant == "empty_signature" else "当前签名",
        customStatus={"faceId": 1, "wording": "synthetic"} if variant == "complete" else None)
    assert len(response["data"]) == (21 if variant == "required_only" else 25)

    def qq(request):
        if request.url.path == profile_probe.ACTION:
            assert json.loads(request.content) == {"user_id": 10002}
            return httpx.Response(200, json=response)

    async def scenario():
        async with source_window(core, tmp_path, silent, live=True, approved=True,
            scope_options=scope, protocol_reply=qq) as (window, client, protocol, _, _):
            result = await profile_probe.collect(window, client, protocol)
            assert result == {"status": "ACCEPTED", "fields": ["nickname", "signature"]}
            rows = {row["field"]: row for row in core.store.rows(
                "SELECT * FROM social_autonomy_profiles")}
            assert rows["nickname"]["value"] == response["data"]["nickname"]
            assert rows["signature"]["value"] == response["data"]["long_nick"]
            assert all(row["receipt_sha256"] == digest(response) for row in rows.values())
            assert not core.store.rows("SELECT * FROM model_usage")
            assert not core.store.rows("SELECT * FROM outbox")
            assert window.db.execute("SELECT COUNT(*) FROM window_attempts").fetchone()[0] == 0

    run(scenario())


@pytest.mark.parametrize("change", [
    "compact", "compact_empty_uid", "missing_uid", "missing_qid", "missing_signature",
    {"uid": None}, {"uid": "unknown"}, {"user_id": 10001}, {"qid": 1},
    {"age": True}, {"qq_level": 12.0}, {"customStatus": []}, {"is_vip": 0},
    {"vip_level": True}, {"reg_time": "1200000000"}, {"qqLevel": 13}, {"ext_status": 1},
    {"unexpected": "field"},
])
def test_empty_uid_does_not_bypass_full_native_profile_contract(core, tmp_path, change):
    scope = options(core, ("nickname", "signature"))
    response = full_profile()
    if change in ("compact", "compact_empty_uid"):
        response["data"] = {key: response["data"][key] for key in (
            "user_id", "nickname", "remark", "sex", "age", "long_nick")}
        if change == "compact_empty_uid":
            response["data"]["uid"] = ""
    elif isinstance(change, str):
        response["data"].pop({"missing_uid": "uid", "missing_qid": "qid",
                              "missing_signature": "long_nick"}[change])
    else:
        response["data"].update(change)

    def qq(request):
        if request.url.path == profile_probe.ACTION:
            return httpx.Response(200, json=response)

    async def scenario():
        async with source_window(core, tmp_path, silent, live=True, approved=True,
            scope_options=scope, protocol_reply=qq) as (window, client, protocol, _, _):
            result = await profile_probe.collect(window, client, protocol)
            assert result["status"] == "UNAVAILABLE"
            assert not core.store.rows("SELECT * FROM social_autonomy_profiles")
            assert not core.store.rows("SELECT * FROM model_usage")
            assert not core.store.rows("SELECT * FROM outbox")
            assert window.row()["state"] == "ACTIVE"

    run(scenario())


def picture():
    output = io.BytesIO()
    Image.new("RGB", (100, 100), "navy").save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize("account_scope", [False, True])
def test_disabled_profile_pump_is_read_only_and_throttled(core, tmp_path, monkeypatch,
                                                        account_scope):
    scope, calls = options(core, ()), []
    moment = [100.0]
    monkeypatch.setattr(profile_probe.time, "monotonic", lambda: moment[0])

    def qq(request):
        pytest.fail("disabled profile probe must not call OneBot")

    async def scenario():
        async with source_window(core, tmp_path, silent, live=True, approved=True,
            scope_options=scope, protocol_reply=qq, account_scope=account_scope) as (
                runtime, client, protocol, _, _):
            async def observe(request):
                calls.append(request.url.path)

            client.event_hooks["request"].append(observe)
            before = runtime.path.read_bytes()
            projection = list(runtime.db.iterdump())
            pump = profile_probe.ProfilePump(runtime, client, protocol)
            pump.tick(idle=True)
            await pump.task
            first = len(calls)
            assert first > 0
            for _ in range(4):
                pump.tick(idle=True)
            assert len(calls) == first and not pump.running
            moment[0] += 60
            pump.tick(idle=True)
            await pump.task
            assert len(calls) == first * 2
            assert runtime.path.read_bytes() == before
            assert list(runtime.db.iterdump()) == projection
            runtime.verify()
            await pump.close()

    run(scenario())


async def public(host, port):
    assert host == "q1.qlogo.cn" and port == 443
    return ["8.8.8.8"]


def test_shared_delivery_protocol_bytes_are_in_bridge_runtime_attestation(monkeypatch):
    target = Path(companion_delivery_protocol.__file__)
    original = Path.read_bytes
    before = bridge_code_sha256()
    monkeypatch.setattr(Path, "read_bytes", lambda path: original(path) + b"\n# changed\n"
                        if path == target else original(path))
    assert bridge_code_sha256() != before


def test_authorized_profile_observations_keep_independent_receipts_and_restart_cooldown(
    core, tmp_path,
):
    scope = options(core)
    native, downloads = [], []
    response, image = profile(), picture()

    def qq(request):
        if request.url.path == profile_probe.ACTION:
            assert json.loads(request.content) == {"user_id": 10002}
            native.append(request)
            return httpx.Response(200, json=response)

    def cdn(request):
        downloads.append(request)
        assert str(request.url) == "https://8.8.8.8/g?b=qq&nk=10002&s=100"
        assert request.headers["Host"] == "q1.qlogo.cn"
        assert request.extensions["sni_hostname"] == "q1.qlogo.cn"
        assert "Authorization" not in request.headers and "Cookie" not in request.headers
        return httpx.Response(200, content=image, headers={"Content-Type": "image/png"})

    async def scenario():
        async with source_window(core, tmp_path, silent, live=True, approved=True,
            scope_options=scope, protocol_reply=qq) as (window, client, protocol, _, _):
            kwargs = {"transport": httpx.MockTransport(cdn), "resolver": public}
            result = await profile_probe.collect(window, client, protocol, **kwargs)
            assert result == {"status": "ACCEPTED", "fields": ["nickname", "signature", "avatar"]}
            rows = {row["field"]: row for row in core.store.rows(
                "SELECT * FROM social_autonomy_profiles")}
            assert rows["nickname"]["value"] == "当前名字"
            assert rows["signature"]["receipt_sha256"] == digest(response)
            assert rows["avatar"]["value"] == hashlib.sha256(image).hexdigest()
            assert rows["avatar"]["receipt_sha256"] != digest(response)
            assert not profile_probe.due(window)
            restarted = profile_probe.ProfilePump(window, client, protocol, **kwargs)
            restarted.tick(idle=True)
            assert not restarted.running
            core.clock.now += timedelta(seconds=60)
            assert (await profile_probe.collect(window, client, protocol, **kwargs))[
                "status"] == "ACCEPTED"
            assert len(native) == len(downloads) == 2
            assert window.db.execute("SELECT COUNT(*) FROM window_inputs").fetchone()[0] == 0
            assert window.db.execute("SELECT COUNT(*) FROM window_attempts").fetchone()[0] == 0
            journal = window.path.read_text("utf-8")
            assert "当前名字" not in journal and "当前签名" not in journal
            window.verify()

    run(scenario())


@pytest.mark.parametrize("fields", [(), ("nickname",), ("avatar",)])
def test_probe_reads_only_authorized_fields_and_qzone_only_has_no_native_io(core, tmp_path, fields):
    scope, calls = options(core, fields), []

    def qq(request):
        if request.url.path == profile_probe.ACTION:
            calls.append("native")
            return httpx.Response(200, json=profile())

    def cdn(request):
        calls.append("avatar")
        return httpx.Response(200, content=picture(), headers={"Content-Type": "image/png"})

    async def scenario():
        async with source_window(core, tmp_path, silent, live=True, approved=True,
            scope_options=scope, protocol_reply=qq) as (window, client, protocol, _, _):
            await profile_probe.collect(window, client, protocol,
                transport=httpx.MockTransport(cdn), resolver=public)
            assert calls == (["native"] if fields == ("nickname",) else
                             ["avatar"] if fields == ("avatar",) else [])
            assert {row["field"] for row in core.store.rows(
                "SELECT * FROM social_autonomy_profiles")} == set(fields)

    run(scenario())


@pytest.mark.parametrize("data", [{"uid": None}, {"user_id": 10001}, {"uid": "unknown"},
                                 {"nickname": "", "long_nick": None}])
def test_native_fallback_and_absent_fields_do_not_become_empty_baselines(core, tmp_path, data):
    scope = options(core, ("nickname", "signature"))

    def qq(request):
        if request.url.path == profile_probe.ACTION:
            return httpx.Response(200, json=profile(**data))

    async def scenario():
        async with source_window(core, tmp_path, silent, live=True, approved=True,
            scope_options=scope, protocol_reply=qq) as (window, client, protocol, _, _):
            assert (await profile_probe.collect(window, client, protocol))["status"] == (
                "UNAVAILABLE")
            assert not core.store.rows("SELECT * FROM social_autonomy_profiles")
            assert window.row()["state"] == "ACTIVE"

    run(scenario())


@pytest.mark.parametrize("failure", ["private_dns", "redirect", "mime", "oversize"])
def test_avatar_failure_keeps_valid_authorized_text_baseline_only(core, tmp_path, failure):
    scope, downloads = options(core), []

    def qq(request):
        if request.url.path == profile_probe.ACTION:
            return httpx.Response(200, json=profile())

    async def dns(host, port):
        return ["127.0.0.1"] if failure == "private_dns" else await public(host, port)

    def cdn(request):
        downloads.append(request)
        return httpx.Response(302 if failure == "redirect" else 200,
            content=picture(), headers={"Content-Type": "text/html" if failure == "mime"
                else "image/png", "Location": "http://127.0.0.1/private",
                **({"Content-Length": "2097153"} if failure == "oversize" else {})})

    async def scenario():
        async with source_window(core, tmp_path, silent, live=True, approved=True,
            scope_options=scope, protocol_reply=qq) as (window, client, protocol, _, _):
            result = await profile_probe.collect(window, client, protocol,
                transport=httpx.MockTransport(cdn), resolver=dns)
            assert result == {"status": "ACCEPTED", "fields": ["nickname", "signature"]}
            assert len(downloads) == (0 if failure == "private_dns" else 1)
            assert not core.store.one("SELECT 1 FROM social_autonomy_profiles WHERE field='avatar'")
            assert window.row()["state"] == "ACTIVE"

    run(scenario())


@pytest.mark.parametrize("change", ["body", "path", "stop", "owner", "stream"])
def test_native_profile_final_guard_runs_after_core_await_and_seals_stream(core, tmp_path, change):
    scope, requests, native = options(core, ("nickname",)), [], []

    def qq(request):
        if request.url.path == profile_probe.ACTION:
            native.append(request)
            return httpx.Response(200, json=profile())

    class Changed(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("replacement stream must not be dispatched")
            yield b"unreachable"

    async def scenario():
        async with source_window(core, tmp_path, silent, live=True, approved=True,
            scope_options=scope, protocol_reply=qq) as (window, client, protocol, _, _):
            async def hold(request):
                if request.url.path == profile_probe.ACTION:
                    requests.append(request)

            async def mutate(response):
                if response.request.url.path != profile_probe.STATE or not requests:
                    return
                request = requests.pop()
                if change == "body":
                    request._content = b'{"user_id":10001}'
                elif change == "path":
                    request.url = request.url.copy_with(path="/send_private_msg")
                elif change == "stream":
                    request.stream = Changed()
                elif change == "stop":
                    window.stop()
                else:
                    from companion_onebot_bridge.event_normalizer import normalize

                    frame = json.dumps(raw_event(2, text="先回应这里",
                        time=int(window.clock().timestamp())))
                    event = normalize(frame, window.config)
                    window.persist_capture(event, frame, received_at=window.clock(),
                                           peer=("127.0.0.1", 9000))

            protocol.event_hooks["request"].append(hold)
            client.event_hooks["response"].append(mutate)
            if change in {"stop", "owner"}:
                with pytest.raises(ValueError):
                    await profile_probe.collect(window, client, protocol)
            else:
                result = await profile_probe.collect(window, client, protocol)
                assert result["status"] == ("ACCEPTED" if change == "stream" else "UNAVAILABLE")
            assert len(native) == int(change == "stream")

    run(scenario())


def test_observation_submission_cannot_be_changed_by_request_hook(core, tmp_path):
    scope = options(core, ("nickname",))

    def qq(request):
        if request.url.path == profile_probe.ACTION:
            return httpx.Response(200, json=profile())

    async def scenario():
        async with source_window(core, tmp_path, silent, live=True, approved=True,
            scope_options=scope, protocol_reply=qq) as (window, client, protocol, _, _):
            async def mutate(request):
                if request.url.path == profile_probe.SUBMIT:
                    material = json.loads(request.content)
                    material["profiles"][0]["value"] = "伪造观测"
                    request._content = json.dumps(material).encode()

            client.event_hooks["request"].append(mutate)
            with pytest.raises(ValueError, match="ACCOUNT_ACTIONS_CORE_REQUEST_CHANGED"):
                await profile_probe.collect(window, client, protocol)
            assert not core.store.rows("SELECT * FROM social_autonomy_profiles")

    run(scenario())


@pytest.mark.parametrize("empty_uid", [False, True])
def test_continuous_probe_real_core_http_independent_worker_and_native_profile_action(
    core, tmp_path, monkeypatch, empty_uid,
):
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)
    scope, native, model_calls = options(core, ("nickname",)), [], []
    from tests.social_profile_support import confirmed_profile_material

    memory = confirmed_profile_material(core)
    policy = scope["core_options"]["social_autonomy"]
    scope["core_options"]["social_autonomy"] = policy.model_copy(update={"qzone_enabled": False})

    def qq(request):
        if request.url.path == profile_probe.ACTION:
            native.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json=full_profile() if empty_uid else profile())
        if request.url.path == "/set_qq_profile":
            native.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": None})

    def answer(request):
        body = json.loads(request.content)
        model_calls.append(body)
        material = json.loads(body["messages"][-1]["content"])
        assert material["purpose"] == "owner_confirmed_profile"
        assert material["owner_confirmed_material"][0]["content"] == "我偏爱星空与简洁的表达。"
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"kind": "nickname", "value": "观星"}, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    async def scenario():
        ready, pause = asyncio.Event(), asyncio.Event()
        async with real_core_http(core) as port, source_window(core, tmp_path, silent,
            live=True, approved=True, scope_options=scope, protocol_reply=qq,
            bridge_options={"core_url": f"http://127.0.0.1:{port}"}) as (
            window, _, protocol, handshakes, _,
        ), private_http_client(window.config.core_url,
            window.config.bridge_token) as client, httpx.AsyncClient(
            transport=httpx.MockTransport(answer)) as external:
            async def observe(response):
                if (response.status_code == 200
                    and response.request.url.path == profile_probe.SUBMIT):
                    ready.set()
                if response.status_code == 200 and response.request.url.path.endswith("/result"):
                    pause.set()

            client.event_hooks["response"].append(observe)
            with Core(core.settings, ModelClient(core.settings, external), core.clock) as worker:
                assert worker.store is not core.store

                async def execute():
                    await ready.wait()
                    from companion.social_autonomy import SocialAutonomy
                    from tests.test_social_autonomy import queue

                    module = SocialAutonomy(worker.store, worker.settings,
                        model=worker.model, clock=worker.clock)
                    planned = await module.plan("native-profile-worker",
                        profile_fields=["nickname"], profile_memory_ids=[memory])
                    conversation = worker.store.one("SELECT conversation_id FROM conversations "
                        "WHERE platform_session_key=?", (window.config.origin,))
                    queue(module, planned, conversation["conversation_id"])

                async with asyncio.timeout(20):
                    from companion_onebot_bridge.app import deliver, observe_profile
                    from companion_onebot_bridge.metrics import Metrics

                    pump = profile_probe.ProfilePump(window, client, protocol)
                    memes = MemePump(window, client)
                    await asyncio.gather(observe_profile(pump, pause, memes), execute(),
                        deliver(client, None, window.spool, pause, Metrics(),
                                runtime=window, protocol=protocol))
            assert handshakes == []
            assert native == [(profile_probe.ACTION, {"user_id": 10002}),
                              ("/set_qq_profile", {"nickname": "观星"})]
            assert len(model_calls) == 1
            row = core.store.one("SELECT state,provider_message_id,attempt_count FROM outbox")
            assert row == {"state": "SENT", "provider_message_id": None, "attempt_count": 1}
            assert window.db.execute("SELECT COUNT(*) FROM window_inputs").fetchone()[0] == 0
            assert window.db.execute(
                "SELECT COUNT(*) FROM window_attempts WHERE state='ACK'").fetchone()[0] == 1
            window.verify()

    run(scenario())

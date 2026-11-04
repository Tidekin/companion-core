"""Real Core CLI/API/worker entry points; only external HTTP and Docker are synthetic."""

import functools
import importlib
import json
import pickle
import sys
from pathlib import Path

import httpx

import companion.api as api_module
from companion import core as core_module
from companion.config import Settings
from companion.retrieval import SemanticIndex
from tests.conftest import Clock
from tests.model_carry_support import observer
from tests.sdk_reader_fixture import answer, call

bundle = pickle.loads(Path(sys.argv[1]).read_bytes())
mode, settings, now = bundle["mode"], bundle["settings"], bundle["now"]
clock = Clock()
clock.now = now
Settings.load = classmethod(lambda cls: settings)
SemanticIndex.warmup = lambda self: None
requests = []


class Finished(BaseException):
    pass


def provider(request):
    requests.append(json.loads(request.content))
    if mode == "worker-timeout":
        assert len(requests) == 1
        raise httpx.ReadTimeout("current native transport ended", request=request)
    if mode == "worker-video":
        if request.url.host == "video.example.test":
            raise httpx.ReadTimeout("synthetic video connection ended", request=request)
        assert len(requests) == 1
        result = answer(calls=[call("old-motion-call", "understand_video", {
            "question": "动作？", "preview_reply": "抽帧里能看到不同颜色。"})])
    else:
        assert mode == "worker-text" and len(requests) == 1
        result = answer("新独立消息可以正常回复。")
    return httpx.Response(200, json=result)


BaseClient = httpx.AsyncClient


class OfflineClient(BaseClient):
    def __init__(self, *args, **kwargs):
        # Keep the real admission transport on internally scoped clients.
        kwargs.setdefault("transport", httpx.MockTransport(provider))
        super().__init__(*args, **kwargs)


def write_output():
    Path(bundle["output"]).write_text(json.dumps(requests), encoding="utf-8")


class TimedCore(core_module.Core):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("clock", clock)
        super().__init__(*args, **kwargs)

    async def run_once(self, *args, **kwargs):
        progressed = await super().run_once(*args, **kwargs)
        if progressed and mode.startswith("worker"):
            write_output()
            print("WORK_COMPLETED", flush=True)
            if mode in {"worker-text", "worker-timeout"}:
                raise Finished()
        return progressed


core_module.Core = TimedCore
api_module.Core = TimedCore
api_module.AsyncClient = OfflineClient
entry = importlib.import_module("companion.__main__")
if mode == "api":
    import uvicorn

    main_module = importlib.import_module("uvicorn.main")

    class ReadyServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            port = self.servers[0].sockets[0].getsockname()[1]
            print(f"API_READY {port}", flush=True)

    main_module.Server = ReadyServer
    sys.argv = ["companion", "serve", "--host", "127.0.0.1", "--port", str(bundle["port"])]
elif mode.startswith("worker"):
    httpx.AsyncClient = OfflineClient
    sys.argv = ["companion", "worker"]
elif mode == "controller":
    import companion.model_carry_controller as controller

    original_verify = controller.verify
    entry.utcnow = clock
    controller.verify = functools.partial(original_verify, observe=observer(bundle["executors"]))
    sys.argv = [
        "companion",
        bundle["command"],
        "--request",
        bundle["request"],
        "--request-sha256",
        bundle["request_sha256"],
    ]
elif mode == "bridge":
    from companion_onebot_bridge import app
    from companion_onebot_bridge.config import BridgeConfig

    if bundle.get("freeze_bridge_clock"):
        from companion_onebot_bridge import owner_runtime

        OriginalRuntime = owner_runtime.OwnerRuntime

        class TimedRuntime(OriginalRuntime):
            def __init__(self, *args, **kwargs):
                kwargs.setdefault("clock", lambda: clock.now)
                super().__init__(*args, **kwargs)

        owner_runtime.OwnerRuntime = TimedRuntime
    BridgeConfig.load = staticmethod(lambda _: bundle["config"])
    sys.argv = ["bridge", "--config", "synthetic-native-config", *bundle["arguments"]]
    entry = app
else:
    raise AssertionError(mode)

try:
    entry.main()
except Finished:
    pass

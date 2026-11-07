import importlib.util
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def bridge_module(tmp_path, monkeypatch):
    modules = {
        name: types.ModuleType(name)
        for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star")
    }
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = types.SimpleNamespace(
        error=lambda *a: None, debug=lambda *a: None, warning=lambda *a: None
    )
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
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda: tmp_path)
    star.register = lambda *a: lambda cls: cls
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    name = "astrbot_plugin_companion_bridge._content_test_main"
    spec = importlib.util.spec_from_file_location(
        name, Path("astrbot_plugin_companion_bridge/main.py")
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def config(**overrides):
    return {
        "owner_qq_id": "10001",
        "bot_qq_id": "10002",
        "bridge_token": "x" * 32,
        **overrides,
    }


def event(order, *, text="看看这个", sender="10001", message_id="42", raw=None):
    return types.SimpleNamespace(
        get_platform_name=lambda: "aiocqhttp",
        get_sender_id=lambda: sender,
        get_group_id=lambda: "",
        unified_msg_origin="qq:FriendMessage:10001",
        message_str=text,
        stop_event=lambda: order.append("stop"),
        message_obj=types.SimpleNamespace(
            self_id="10002",
            message_id=message_id,
            timestamp=1792382400,
            raw_message=raw or {},
        ),
    )

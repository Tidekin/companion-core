"""A meme source may be a signed native-client message, attributed to its own device."""

from types import SimpleNamespace

import pytest

from companion.meme_assets import NATIVE_SESSION, custody_for

DEVICE = "a" * 32
SETTINGS = SimpleNamespace(owner_qq_id="10001", bot_qq_id="20002")


def view(devices=None, capture="bridge-key"):
    return SimpleNamespace(policy=SimpleNamespace(
        native_devices={d: SimpleNamespace(public_key=k) for d, k in (devices or {}).items()},
        capture=SimpleNamespace(public_key=capture) if capture else None))


def capture(source_event_id, epoch):
    return SimpleNamespace(source_event_id=source_event_id, protocol_epoch=epoch)


def test_native_custody_names_the_signing_device():
    value = custody_for(SETTINGS, "native.chat", NATIVE_SESSION,
                        capture(f"native:{DEVICE}:{'b' * 32}", "native-v1"),
                        view({DEVICE: "device-key"}))
    assert value == {"identity": ["native.chat", DEVICE, NATIVE_SESSION, "native-v1"],
                     "capture_public_key": "device-key"}


def test_qq_custody_is_unchanged():
    session = "onebot-direct:FriendMessage:10001"
    assert custody_for(SETTINGS, "qq.onebot", session, capture("x", "epoch-1"), view()) == {
        "identity": ["10001", "20002", session, "epoch-1"], "capture_public_key": "bridge-key"}


@pytest.mark.parametrize("case", ["unknown_device", "wrong_session", "other_source", "no_bridge"])
def test_unattributable_sources_are_refused(case):
    native = capture(f"native:{DEVICE}:{'b' * 32}", "native-v1")
    args = {
        "unknown_device": ("native.chat", NATIVE_SESSION, native, view({})),
        "wrong_session": ("native.chat", "native:other", native, view({DEVICE: "k"})),
        "other_source": ("system.timer", NATIVE_SESSION, native, view({DEVICE: "k"})),
        "no_bridge": ("qq.onebot", "s", capture("x", "e"), view(capture=None)),
    }[case]
    with pytest.raises(ValueError, match="MEME_ASSET_SOURCE_UNATTRIBUTED"):
        custody_for(SETTINGS, *args)

import pytest

from companion_voice_node.applio_render import ApplioRenderConfig
from companion_voice_node.providers import LocalVoiceError
from companion_voice_node.resources import file_digest


def resources(tmp_path):
    config = ApplioRenderConfig(applio_source=tmp_path / "applio",
        separator_source=tmp_path / "uvr", voice_model=tmp_path / "voice.pth",
        voice_index=tmp_path / "voice.index", separator_model=tmp_path / "separator.pth",
        resource_sha256={str(index): "0" * 64 for index in range(10)})
    for name, path in config.resources().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    return config.model_copy(update={"resource_sha256": {
        name: file_digest(path)["sha256"] for name, path in config.resources().items()}})


def test_pinned_voice_and_processor_changes_are_rejected_before_native_loading(tmp_path):
    config = resources(tmp_path)
    original = config.verify()
    config.voice_model.write_bytes(b"a different voice model")
    with pytest.raises(LocalVoiceError, match="SINGING_RESOURCE_CHANGED"):
        config.verify()
    assert len(original) == 10


def test_resource_set_is_closed(tmp_path):
    config = resources(tmp_path)
    changed = {**config.resource_sha256}
    changed.pop("separator_model")
    changed["arbitrary_plugin"] = "0" * 64
    with pytest.raises(LocalVoiceError, match="SINGING_RESOURCE_SET_INVALID"):
        config.model_copy(update={"resource_sha256": changed}).verify()

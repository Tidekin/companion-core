import pytest

from companion_voice_node.providers import LocalVoiceError
from tests.support.applio_render import resources


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

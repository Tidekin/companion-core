from companion_voice_node.applio_render import ApplioRenderConfig
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

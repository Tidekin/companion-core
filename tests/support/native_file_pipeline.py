from companion.config import ObservationAdapterConfig


def configure(core):
    core.settings.observation_enabled = True
    core.settings.observation_adapters = {"content_file": ObservationAdapterConfig(
        source="qq_file", token="synthetic-file-adapter-token-12345")}

from companion.config import ObservationAdapterConfig
from tests.model_carry_support import migrated
from tests.owner_model_fixtures import authorized_job
from tests.support.bili_video_entrypoint import TOKEN
from tests.support.exploration import configure
from tests.support.paid_model import approved_live


def reader_carry(core, tmp_path, **limits):
    configure(core)
    core.settings.content_enabled = True
    core.settings.rich_input_scope = "references_v1"
    core.settings.observation_adapters["bili"] = ObservationAdapterConfig(
        source="bilibili", token=TOKEN, reader_max_requests=6)
    approved_live(core, tmp_path, concurrency=1, **limits)
    authorized_job(core, purposes=("chat", "content", "exploration"))
    return migrated(core, tmp_path, **limits)

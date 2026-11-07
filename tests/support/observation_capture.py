import hashlib

from pydantic import SecretStr

from companion.config import ObservationAdapterConfig
from companion.observation import ObservationEvent
from companion.privacy import epoch


def enable_observations(core, source="simulation", media=False):
    core.settings.observation_enabled = True
    core.settings.control_token = SecretStr("owner-control-" * 4)
    core.settings.observation_adapters = {
        "reader": ObservationAdapterConfig(
            token="reader-token-" * 4, source=source, media_analysis=media
        )
    }


def observation(core, number=1, text="已获取的文章内容", **changes):
    return ObservationEvent.model_validate(
        {
            "schema_version": 2,
            "event_id": f"obs_test_{number}",
            "event_type": "content.observed",
            "source": "simulation",
            "source_kind": "text",
            "provider_event_id": str(number),
            "occurred_at": core.clock(),
            "trace_id": f"trace_obs_{number}",
            "result_status": "FETCHED",
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "privacy_epoch": epoch(core.store),
            "payload": {"text": text, "url": "https://example.org/article"},
            **changes,
        }
    )

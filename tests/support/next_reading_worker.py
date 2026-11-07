from datetime import datetime, timedelta

from tests.support.next_reading import WEB
from tests.support.source_reader import ITEM, NOW

NOW_DT = datetime.fromisoformat(NOW)


TOKEN = "isolated-reader-core-token-only"


def lease(source="web", **changes):
    return {
        **ITEM, "schema_version": 1, "request_id": "read_fixture_v1",
        "lease_until": (NOW_DT + timedelta(seconds=30)).isoformat(),
        "url": WEB if source == "web" else ITEM["url"],
        "context": {"root_task_id": "root_fixture_v1", "source": source,
                    "source_event_ids": ["evt_owner_1"], "privacy_epoch": 3},
        "allowed_hosts": ["articles.example"] if source == "web" else [],
        **changes,
    }

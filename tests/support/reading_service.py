from companion.config import ObservationAdapterConfig
from companion.source_grants import SourceGrantRegistration
from tests.support.exploration import configure as configure_legacy
from tests.support.reading_protocol import envelope
from tests.support.reply_revision import accept

TOKEN = "native-core-web-reader-token-" * 3


def configure(core):
    configure_legacy(core)
    core.settings.content_enabled = core.settings.web_reading_enabled = True
    core.settings.web_reading_hosts = ("articles.example",)
    core.settings.observation_adapters["web"] = ObservationAdapterConfig(source="web", token=TOKEN)


def queued(core, source="web", *, owner=True, hosts=None, grant=None):
    configure(core)
    if hosts is not None:
        core.settings.web_reading_hosts = hosts
    adapter = {"web": "web", "bilibili": "bili", "heybox": "heybox"}[source]
    if grant is not None:
        core.settings.observation_adapters[adapter].source_grant_required = True
        core.observations.grants.register(
            SourceGrantRegistration(adapter_id=adapter, **grant.snapshot()))
    body = envelope(source, grant=grant.snapshot() if grant else None)
    if owner:
        accept(core, text="请阅读 " + body["observations"][0]["url"])
    row = core.store.one("SELECT request_id FROM exploration_requests ORDER BY rowid DESC")
    if not row:
        with core.store.transaction():
            request = core.exploration.create(
                adapter, "read", "test_native_read", body["observations"][0]["url"],
                source_event_ids=["evt_test_1"] if owner else [],
            )
    else:
        request = row["request_id"]
    item = core.reading.lease(adapter, grant)
    assert item and item["request_id"] == request
    body["lease_token"] = item["lease_token"]
    body["observations"][0]["provenance"].update(item["context"])
    body["observations"][0]["fetched_at"] = core.clock()
    body["observations"][0]["evidence"]["fetched_at"] = core.clock()
    return adapter, item, body

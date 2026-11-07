from contextlib import asynccontextmanager

from companion import meme_service
from tests.meme_authority_fixture import admit, environment


@asynccontextmanager
async def discovery_environment(core, tmp_path, monkeypatch, *, paid=None, purposes=(),
                                history=None, **changes):
    core.settings = core.settings.model_copy(update={
        "meme_discovery_enabled": True,
        "meme_discovery_daily_queries": 2,
        "meme_discovery_daily_download_bytes": 32 * 1024 * 1024,
        "meme_discovery_daily_storage_bytes": 8 * 1024 * 1024,
        "expression_selection_probability": 1.0,
        **changes,
    })
    async with environment(core, tmp_path, monkeypatch, paid=paid,
                           purposes=purposes, history=history) as values:
        client, config, plan, now = values
        event, accepted = await admit(client, config, plan, now)
        assert accepted.status_code == 202, accepted.text
        # Background availability is a unit-test precondition. Other tests run
        # the real reply model and the independent worker through this entry point.
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?",
                           (accepted.json()["job_id"],))
        yield event, accepted.json(), values


def submit(core, event, *, provider="community"):
    return meme_service.submit(core.store, core.settings, core.clock, meme_service.DiscoveryRequest(
        event_id=event["event_id"], reply_revision=1, intent="celebrate", provider=provider,
    ))


def claim(core):
    value = meme_service.lease(core.store, core.settings, core.clock,
                               meme_service.DiscoveryLeaseRequest(worker_id="meme-worker"))
    assert value["item"] is not None
    return value["item"]


def reservation(core, item, **limits):
    return meme_service.reserve(core.store, core.settings, core.clock, item["job_id"],
        meme_service.DiscoveryReserve(lease_token=item["lease_token"],
            attempt_key=item["attempt_key"],
            limits=meme_service.DiscoveryLimits(**limits)))


def result(core, item, outcome="SETTLED", **usage):
    return meme_service.result(core.store, core.settings, core.clock, item["job_id"],
        meme_service.DiscoveryResult(lease_token=item["lease_token"],
            attempt_key=item["attempt_key"],
            outcome=outcome, usage=meme_service.DiscoveryUsage(**{
                "queries": 1, "requests": 2, "download_bytes": 128, "storage_bytes": 64,
                "model_cost": 0, **usage,
            })))

"""Optional capability absence keeps native chat; frozen responsibility never disappears."""

import json

import pytest

from companion import owner_authority
from companion.contracts import CoreError
from companion.core import Core
from companion.meme_retrieval import MemeRetrieval
from companion.owner_authority_protocol import AuthorityStop
from companion.privacy import advance_epoch
from companion.store import encode
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, worker_for
from tests.support.meme_core_runtime import runtime_case as runtime_case


@pytest.mark.parametrize(
    "runtime_case",
    ["chat_only", "no_meme", "no_expression", "semantics_off", "expression_off"],
    indirect=True,
)
def test_current_native_chat_without_optional_capability_stays_text(runtime_case, monkeypatch):
    c = runtime_case
    before = c.core.store.rows("SELECT * FROM model_usage")
    captions = c.core.store.rows("SELECT * FROM meme_captions")
    assets = c.core.store.rows("SELECT * FROM meme_assets")

    def forbidden(*args, **kwargs):
        raise AssertionError("An absent optional capability scanned candidates")

    monkeypatch.setattr(MemeRetrieval, "_resolve", forbidden)
    # Release only the test setup's unused native lease; no model/transport started.
    c.core.store.execute(
        "UPDATE jobs SET state='QUEUED',lease_token=NULL WHERE job_id=%s", (c.job["job_id"],)
    )

    async def scenario():
        provider = Provider(answer("在这里陪着你。"))
        async with worker_for(c.core, provider) as worker:
            assert await worker.run_once("optional-core-chat")
        assert len(provider.requests) == 1
        assert c.item["asset_sha256"] not in json.dumps(provider.requests)
        row = c.core.store.one("SELECT * FROM outbox")
        assert row and json.loads(row["content_json"]) == {"text": "在这里陪着你。"}
        assert not c.core.store.one(
            'SELECT 1 FROM runtime_meta WHERE key=%s', ("meme_offer:" + c.job["event_id"],)
        )
        usage = c.core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence')
        assert len(usage) == len(before) + 1 and usage[0] == before[0]
        assert c.core.store.rows("SELECT * FROM meme_captions") == captions
        assert c.core.store.rows("SELECT * FROM meme_assets") == assets

    run(scenario())


@pytest.mark.parametrize("runtime_case", ["no_match"], indirect=True)
@pytest.mark.parametrize("change", ["semantics", "expression", "mode", "epoch", "policy", "stop"])
def test_even_frozen_null_offer_refuses_later_revocation(runtime_case, change):
    c = runtime_case
    run(c.retrieval.prepare(c.job))
    assert c.retrieval.check(c.job)["offer"] is None
    if change == "semantics":
        c.core.settings.meme_semantics_enabled = False
    elif change == "expression":
        c.core.settings.expression_enabled = False
    elif change == "mode":
        c.core.settings.meme_catalog_owner = "bridge"
    elif change == "epoch":
        with c.core.store.transaction(control_write=True):
            advance_epoch(c.core.store)
    elif change == "policy":
        c.core.settings.meme_cooldown_seconds = 1
    else:
        owner_authority.stop(
            c.core.store,
            c.core.settings,
            c.core.clock,
            AuthorityStop(operation_id="stop-null-offer", authority=c.grant.authority),
        )
    with Core(c.core.settings, clock=c.core.clock) as worker:
        with pytest.raises(CoreError):
            worker._check_context_snapshots(c.job, [])
    assert not c.core.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("runtime_case", ["no_meme", "no_expression"], indirect=True)
@pytest.mark.parametrize("record", [None, {"unknown_binding": True}])
def test_existing_opaque_or_null_record_cannot_be_skipped_for_missing_scope(runtime_case, record):
    c = runtime_case
    c.core.store.insert("runtime_meta", key="meme_offer:" + c.job["event_id"], value=encode(record))
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_PURPOSE_REQUIRED"):
        c.service.check_offer(c.job)

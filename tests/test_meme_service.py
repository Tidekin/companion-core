"""Current-turn selection and durable, separately metered public discovery jobs."""

import json
from contextlib import asynccontextmanager, closing

import httpx
import pytest
from pydantic import ValidationError

from companion import meme_service
from companion.contracts import CoreError
from companion.core import Core
from companion.expression import ExpressionEngine, ImageIntent, MemeOffer
from companion.model import ModelClient
from companion.store import Store, encode
from tests.conftest import ingest, run
from tests.meme_authority_fixture import admit, environment, notice, stop


def offer(event_id, *, candidate="a" * 64, category="happy", intent="celebrate"):
    return {"offer_key": "meme:" + event_id, "intent": intent, "candidates": [{
        "candidate_id": candidate, "description": "一只开心挥手的小猫", "tags": ["开心", "庆祝"],
        "category": category,
    }]}


def expression(core, *, probability=1, text="这次项目完成了"):
    ingest(core, text=text)
    core.store.insert("runtime_meta", key="live_text_input:evt_test_1", value=encode({
        "event_id": "evt_test_1", "meme_offer": offer("evt_test_1"),
    }))
    settings = core.settings.model_copy(update={
        "expression_enabled": True, "expression_selection_probability": probability,
    })
    return ExpressionEngine(core.store, settings, core.clock)


def test_current_offer_selects_only_a_public_opaque_id_without_an_extra_model_call(core):
    engine = expression(core)
    instruction = engine.instruction("evt_test_1")
    assert "candidate_id" in instruction and "a" * 64 in instruction
    assert "不可信资料" in instruction and "这次项目完成了" not in instruction
    intent = engine.intent("evt_test_1", "太棒了。⬡" + "a" * 64 + "⬡")
    assert intent.model_dump(exclude_none=True) == {"text": "太棒了。", "image": {
        "source": "companion.meme_catalog", "category": "happy", "candidate_id": "a" * 64,
        "offer_key": "meme:evt_test_1",
    }}
    assert core.model.calls == []


@pytest.mark.parametrize("marker", ["happy", "none", "b" * 64, "https://example.com/image.png"])
def test_offer_never_falls_back_to_a_category_or_foreign_candidate(core, marker):
    intent = expression(core).intent("evt_test_1", "完整文字。⬡" + marker + "⬡")
    assert intent.text == "完整文字。" and intent.image is None


@pytest.mark.parametrize("change", ["duplicate", "wrong_key", "extra_url"])
def test_malformed_offer_is_bounded_and_cannot_enable_the_legacy_selection_path(core, change):
    engine = expression(core)
    value = offer("evt_test_1")
    if change == "duplicate":
        value["candidates"] *= 2
    elif change == "wrong_key":
        value["offer_key"] = "meme:evt_other"
    else:
        value["candidates"][0]["url"] = "https://example.com/private"
    core.store.execute("UPDATE runtime_meta SET value=%s WHERE key='live_text_input:evt_test_1'",
                       (encode({"event_id": "evt_test_1", "meme_offer": value}),))
    assert engine.intent("evt_test_1", "完整文字。⬡happy⬡").image is None
    assert "本轮只回复文字" in engine.instruction("evt_test_1")


@pytest.mark.parametrize("case", ["serious", "probability", "superseded", "recalled"])
def test_new_offer_keeps_serious_probability_revision_and_erasure_gates(core, case):
    engine = expression(core, probability=0 if case == "probability" else 1,
                        text="家人住院了" if case == "serious" else "有个好消息")
    if case == "superseded":
        core.store.execute("UPDATE conversations SET reply_revision=1")
    elif case == "recalled":
        core.store.execute("UPDATE events SET redacted=1,body_json='{}'")
    assert engine.intent("evt_test_1", "我听见了。⬡" + "a" * 64 + "⬡").image is None


def test_new_source_requires_both_bindings_and_probability_is_stable(core):
    with pytest.raises(ValidationError):
        ImageIntent(source="companion.meme_catalog", category="happy")
    with pytest.raises(ValidationError):
        ImageIntent(category="happy", candidate_id="a" * 64, offer_key="meme:evt_test_1")
    engine = expression(core, probability=0.35)
    selected = engine.intent("evt_test_1", "完整文字。⬡" + "a" * 64 + "⬡").image
    with closing(Store(core.settings.database)) as reopened:
        restarted = ExpressionEngine(reopened, engine.settings, core.clock)
        assert restarted.intent("evt_test_1", "完整文字。⬡" + "a" * 64 + "⬡").image == selected


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
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s",
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


def test_discovery_is_disabled_by_default_and_rejects_unbounded_or_paid_input(core):
    with pytest.raises(CoreError, match="MEME_DISCOVERY_DISABLED"):
        meme_service.submit(core.store, core.settings, core.clock, meme_service.DiscoveryRequest(
            event_id="evt_test_1", reply_revision=0, intent="celebrate", provider="community",
        ))
    for value in (True, 0, 2):
        with pytest.raises(ValidationError):
            meme_service.DiscoveryLimits(queries=value)
    with pytest.raises(ValidationError):
        meme_service.DiscoveryUsage(queries=1, requests=1, download_bytes=1,
                                    storage_bytes=1, model_cost=1)
    with pytest.raises(ValidationError):
        MemeOffer.model_validate(offer("evt_test_1") | {"intent": "private chat text"})


def test_discovery_uses_native_jobs_and_a_single_consumable_attempt(core, tmp_path, monkeypatch):
    async def scenario():
        async with discovery_environment(core, tmp_path, monkeypatch) as (event, _, _):
            created = submit(core, event)
            assert submit(core, event) == {**created, "duplicate": True}
            assert core.claim_job("chat-worker") is None
            item = claim(core)
            assert item["job_id"] == created["job_id"]
            first = reservation(core, item)
            assert first["state"] == "RESERVED"
            with pytest.raises(CoreError, match="MEME_DISCOVERY_ATTEMPT_ALREADY_RESERVED"):
                reservation(core, item)
            check = meme_service.check(core.store, core.settings, core.clock, item["job_id"],
                meme_service.DiscoveryCheck.model_validate({key: item[key] for key in (
                    "lease_token", "attempt_key",
                )}))
            assert check["event_id"] == event["event_id"] and check["reply_revision"] == 1
            settled = result(core, item)
            assert settled["state"] == "SETTLED" and result(core, item) == settled
            with pytest.raises(CoreError, match="MEME_DISCOVERY_RESULT_CONFLICT"):
                result(core, item, download_bytes=127)
            assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (item["job_id"],)) == {
                "state": "SUCCEEDED",
            }
            assert not core.store.one("SELECT 1 FROM model_usage")

    run(scenario())


@pytest.mark.parametrize("dimension", ["queries", "download_bytes", "storage_bytes"])
def test_unknown_retains_reserved_daily_quota(core, tmp_path, monkeypatch, dimension):
    caps = {"queries": 1, "download_bytes": 16 * 1024 * 1024, "storage_bytes": 4 * 1024 * 1024}

    async def scenario():
        async with discovery_environment(core, tmp_path, monkeypatch,
            **{"meme_discovery_daily_" + dimension: caps[dimension]}) as (event, _, _):
            submit(core, event)
            first = claim(core)
            reservation(core, first)
            result(core, first, "UNKNOWN", queries=0, requests=0, download_bytes=0, storage_bytes=0)
            submit(core, event, provider="brave")
            second = claim(core)
            with pytest.raises(CoreError, match="MEME_DISCOVERY_DAILY_" + dimension.upper()):
                reservation(core, second)
            assert core.store.one("SELECT COUNT(*) AS n FROM meme_discovery_reservations")["n"] == 1

    run(scenario())


def test_expired_reserved_lease_survives_restart_as_unknown_without_replay(
    core, tmp_path, monkeypatch,
):
    async def scenario():
        async with discovery_environment(core, tmp_path, monkeypatch) as (event, _, _):
            submit(core, event)
            item = claim(core)
            reservation(core, item)
            core.clock.advance(core.settings.lease_seconds + 1)
            reopened = Store(core.settings.database)
            try:
                meme_service.recover(reopened, core.settings, core.clock())
                assert reopened.one("SELECT state FROM meme_discovery_reservations") == {
                    "state": "UNKNOWN",
                }
                assert meme_service.lease(reopened, core.settings, core.clock,
                    meme_service.DiscoveryLeaseRequest(worker_id="restarted")) == {"item": None}
            finally:
                reopened.close()
            assert result(core, item)["state"] == "UNKNOWN"
            observed = core.store.one(
                "SELECT reserved_queries,queries FROM meme_discovery_reservations",
            )
            assert observed == {
                "reserved_queries": 1, "queries": 1,
            }

    run(scenario())


@pytest.mark.parametrize("boundary", ["stop", "recall", "new_input"])
def test_revocation_blocks_network_checks_but_accepts_original_attempt_accounting(
    core, tmp_path, monkeypatch, boundary,
):
    async def scenario():
        async with discovery_environment(core, tmp_path, monkeypatch) as (event, _, values):
            client, config, plan, now = values
            submit(core, event)
            item = claim(core)
            reservation(core, item)
            if boundary == "stop":
                stopped = await stop(client, plan)
                assert stopped.status_code == 200, stopped.text
            elif boundary == "recall":
                recalled = await client.post("/internal/v1/owner-authority/inputs/recall",
                                             json=notice(config, plan, now))
                assert recalled.status_code == 200, recalled.text
            else:
                _, accepted = await admit(client, config, plan, now, number=2)
                assert accepted.status_code == 202, accepted.text
            with pytest.raises(CoreError):
                meme_service.check(core.store, core.settings, core.clock, item["job_id"],
                    meme_service.DiscoveryCheck(lease_token=item["lease_token"],
                                                attempt_key=item["attempt_key"]))
            assert result(core, item, "UNKNOWN")["state"] == "UNKNOWN"
            forged = dict(item, lease_token="lease_" + "f" * 32)
            with pytest.raises(CoreError, match="MEME_DISCOVERY_STALE_LEASE"):
                result(core, forged)
            assert not core.store.one("SELECT 1 FROM model_usage")

    run(scenario())


def test_observed_overrun_is_recorded_unknown_and_zero_usage_can_settle(
    core, tmp_path, monkeypatch,
):
    async def scenario():
        async with discovery_environment(core, tmp_path, monkeypatch) as (event, _, _):
            submit(core, event)
            first = claim(core)
            reservation(core, first, download_bytes=128)
            assert result(core, first, download_bytes=129)["state"] == "UNKNOWN"
            assert core.store.one("SELECT download_bytes FROM meme_discovery_reservations") == {
                "download_bytes": 129,
            }
            submit(core, event, provider="brave")
            second = claim(core)
            reservation(core, second)
            assert result(core, second, queries=0, requests=0, download_bytes=0,
                          storage_bytes=0)["state"] == "SETTLED"

    run(scenario())


def test_native_offer_is_bound_before_same_call_model_selection(core, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from companion import meme_core
    from tests.test_meme_core import CAPTION, body, caption_environment, worker

    monkeypatch.setattr(meme_core, "encoder", lambda *args, **kwargs:
        SimpleNamespace(query_embed=lambda text: [[1.0] + [0.0] * 511]))

    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            source, _, client, config, grant, now, item = values
            created = await client.post("/internal/v1/memes/captions",
                                         json=body(core, source, item, now))
            assert created.status_code == 200, created.text
            caption_calls = []
            assert await worker(core, caption_calls)
            asset = created.json()["asset_sha256"]
            event, accepted = await admit(client, config, grant, now, 2, text="我想要抱抱")
            assert accepted.status_code == 202, accepted.text
            query = await client.post("/internal/v1/memes/query", json={
                "event_id": event["event_id"], "reply_revision": 2})
            assert query.status_code == 200, query.text
            candidate = offer(event["event_id"], candidate=asset, category="like", intent="comfort")
            candidate["candidates"][0].update(description=CAPTION["description"],
                                                tags=CAPTION["tags"])
            bound = await client.post("/internal/v1/memes/offers", json={
                **{key: query.json()[key] for key in (
                    "job_id", "event_id", "reply_revision", "query_sha256")}, "offer": candidate})
            assert bound.status_code == 200, bound.text
            calls = []

            def provider(request):
                calls.append(json.loads(request.content))
                return httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "content": "给你抱抱。⬡" + asset + "⬡"}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as model_http:
                model = ModelClient(core.settings, model_http)
                with Core(core.settings, model, core.clock) as independent:
                    assert await independent.run_once()
            assert len(caption_calls) == len(calls) == 1
            assert asset in encode(calls[0]["messages"])
            content = json.loads(core.store.one("SELECT content_json FROM outbox")["content_json"])
            assert content["image"] == {
                "source": "companion.meme_catalog", "category": "like", "candidate_id": asset,
                "offer_key": "meme:" + event["event_id"],
            }
            assert len(core.store.rows("SELECT * FROM model_usage WHERE state='SETTLED'")) == 2

    run(scenario())


def test_private_companion_without_an_offer_never_uses_legacy_category_selection(
    core, tmp_path, monkeypatch,
):
    async def scenario():
        async with environment(core, tmp_path, monkeypatch) as (client, config, plan, now):
            event, accepted = await admit(client, config, plan, now)
            assert accepted.status_code == 202, accepted.text
            engine = ExpressionEngine(core.store, core.settings, core.clock)
            assert "本轮只回复文字" in engine.instruction(event["event_id"])
            assert "可用类别" not in engine.instruction(event["event_id"])
            intent = engine.intent(event["event_id"], "一条完整的文字回复。⬡happy⬡")
            assert intent.text == "一条完整的文字回复。" and intent.image is None
            assert not core.store.one("SELECT 1 FROM model_usage")

    run(scenario())


def test_http_discovery_contract_requires_auth_and_settles_the_same_attempt(
    core, tmp_path, monkeypatch,
):
    async def scenario():
        async with discovery_environment(core, tmp_path, monkeypatch) as (event, _, values):
            client, _, _, _ = values
            trigger = {"event_id": event["event_id"], "reply_revision": 1,
                       "intent": "celebrate", "provider": "community"}
            path = "/internal/v1/memes/discovery"
            denied = await client.post(path + "/submit", json=trigger,
                                       headers={"Authorization": "Bearer wrong"})
            assert denied.status_code == 401
            created = await client.post(path + "/submit", json=trigger)
            assert created.status_code in {200, 202}, created.text
            leased = await client.post(path + "/lease", json={"worker_id": "public-meme-worker"})
            assert leased.status_code == 200, leased.text
            item = leased.json()["item"]
            assert item["job_id"] == created.json()["job_id"]
            witness = {key: item[key] for key in ("lease_token", "attempt_key")}
            bound = path + "/" + item["job_id"]
            checked = await client.post(bound + "/check", json=witness)
            assert checked.status_code == 200 and checked.json()["approved"] is True, checked.text
            reserved = await client.post(bound + "/reserve", json=witness | {
                "limits": meme_service.DiscoveryLimits().model_dump(),
            })
            assert reserved.status_code == 200 and reserved.json()["state"] == "RESERVED"
            duplicate = await client.post(bound + "/reserve", json=witness | {
                "limits": meme_service.DiscoveryLimits().model_dump(),
            })
            assert duplicate.status_code == 409
            receipt = witness | {"outcome": "SETTLED", "usage": {
                "queries": 0, "requests": 0, "download_bytes": 0, "storage_bytes": 0,
                "model_cost": 0,
            }}
            result = await client.post(bound + "/result", json=receipt)
            assert result.status_code == 200 and result.json()["state"] == "SETTLED", result.text
            repeated = await client.post(bound + "/result", json=receipt)
            assert repeated.status_code == 200 and repeated.json() == result.json()
            assert core.store.rows("SELECT * FROM outbox") == []
            assert core.store.rows("SELECT * FROM model_usage") == []

    run(scenario())


def test_unreserved_expired_claim_gets_a_new_lease_and_rejects_old_witness(
    core, tmp_path, monkeypatch,
):
    async def scenario():
        async with discovery_environment(core, tmp_path, monkeypatch) as (event, _, _):
            submit(core, event)
            first = claim(core)
            core.clock.advance(core.settings.lease_seconds + 1)
            second = claim(core)
            assert first["job_id"] == second["job_id"]
            assert first["lease_token"] != second["lease_token"]
            assert first["attempt_key"] != second["attempt_key"]
            with pytest.raises(CoreError, match="MEME_DISCOVERY_STALE_LEASE"):
                reservation(core, first)
            assert reservation(core, second)["state"] == "RESERVED"
            assert core.store.one("SELECT COUNT(*) n FROM meme_discovery_reservations")["n"] == 1

    run(scenario())

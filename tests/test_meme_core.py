"""Caption HTTP admission, independent native worker and immutable offer bindings."""

import asyncio
import base64
import io
import json
import random
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from companion import budget, meme_core
from companion.contracts import CoreError
from companion.core import Core
from companion.store import after
from companion_meme_protocol import CaptionOutcome, digest
from tests import meme_authority_fixture
from tests.conftest import run
from tests.meme_authority_fixture import admit, notice, stop
from tests.support.meme_core import CAPTION, body, caption_environment, worker
from tests.support.meme_service import claim, reservation, result


def test_http_native_caption_is_idempotent_and_uses_exact_shared_sampling(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, _, client, _, _, now, item = values
            payload = body(core, event, item, now)
            first = await client.post("/internal/v1/memes/captions", json=payload)
            assert first.status_code == 200, first.text
            queued = CaptionOutcome.model_validate_json(first.text)
            assert queued.state == "QUEUED" and queued.usage is None
            repeated = await client.post("/internal/v1/memes/captions", json=payload)
            assert repeated.json() == first.json()
            calls = []
            assert await worker(core, calls)
            done = await client.post("/internal/v1/memes/captions/" + queued.job_id, json={})
            assert done.status_code == 200, done.text
            outcome = CaptionOutcome.model_validate_json(done.text)
            assert outcome.state == "SUCCEEDED", done.text
            assert outcome.usage.state == "SETTLED" and outcome.usage.billed_micro_usd > 0
            assert outcome.result.sampled_frames.total_frames == 1
            assert outcome.result.sampled_frames.coverage == "complete"
            assert len(calls) == 1 and calls[0]["response_format"] == {"type": "json_object"}
            assert calls[0]["messages"][0]["content"][1]["image_url"]["url"].startswith(
                "data:image/png;base64,"
            )
            assert not core.store.rows("SELECT * FROM outbox")
            assert not await worker(core, calls)
            assert len(core.store.rows("SELECT * FROM model_usage")) == 1

    run(scenario())


def test_caption_capacity_wait_keeps_original_job_and_sends_once(core, tmp_path, monkeypatch):
    from companion import budget

    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, _, client, _, _, now, item = values
            created = await client.post("/internal/v1/memes/captions",
                                        json=body(core, event, item, now))
            assert created.status_code == 200, created.text
            job_id = created.json()["job_id"]
            calls = []

            def full(*args, **kwargs):
                raise CoreError("MODEL_PROVIDER_CONCURRENCY_EXHAUSTED", 429)

            with monkeypatch.context() as patch:
                patch.setattr(budget, "reserve", full)
                assert await worker(core, calls)
            assert not calls and not core.store.rows("SELECT * FROM model_usage")
            assert core.store.one('SELECT state,attempts FROM jobs WHERE job_id=%s', (job_id,)) == {
                "state": "QUEUED", "attempts": 0}
            core.clock.advance(2)
            assert await worker(core, calls)
            assert len(calls) == 1
            assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (job_id,))["state"] == (
                "SUCCEEDED")

    run(scenario())


@pytest.mark.parametrize(
    "mutation",
    [
        "bytes",
        "source_hash",
        "acquired_hash",
        "private_ip",
        "unknown_license",
        "parent_unknown",
        "revision",
        "profile",
    ],
)
def test_caption_rejects_wrong_media_source_or_authority_before_admission(
    core,
    tmp_path,
    monkeypatch,
    mutation,
):
    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, _, client, _, _, now, item = values
            payload = body(core, event, item, now)
            if mutation == "bytes":
                payload["asset"]["data_base64"] = base64.b64encode(b"not an image").decode()
            elif mutation == "source_hash":
                payload["asset"]["source_evidence_sha256"] = "1" * 64
            elif mutation == "acquired_hash":
                payload["source_evidence"]["acquisition"]["image_sha256"] = "2" * 64
            elif mutation == "private_ip":
                payload["source_evidence"]["acquisition"]["fetch"]["hops"][0]["address"] = (
                    "127.0.0.1"
                )
            elif mutation == "unknown_license":
                payload["source_evidence"]["license"] = "unknown"
            elif mutation == "parent_unknown":
                core.store.execute("UPDATE meme_discovery_reservations SET state='UNKNOWN'")
            elif mutation == "revision":
                payload["context"]["reply_revision"] = 2
            else:
                payload["identity"]["model_version"] = "unapproved/model"
            if mutation in {"acquired_hash", "private_ip", "unknown_license"}:
                payload["asset"]["source_evidence_sha256"] = digest(payload["source_evidence"])
            rejected = await client.post("/internal/v1/memes/captions", json=payload)
            assert rejected.status_code in {400, 403, 409}, rejected.text
            assert not core.store.rows("SELECT * FROM meme_captions")
            assert not core.store.rows("SELECT * FROM model_usage")

    run(scenario())


@pytest.mark.parametrize("interruption", ["recall", "stop", "new_input"])
def test_late_caption_keeps_usage_but_never_commits_after_source_change(
    core,
    tmp_path,
    monkeypatch,
    interruption,
):
    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, _, client, config, plan, now, item = values
            created = await client.post(
                "/internal/v1/memes/captions", json=body(core, event, item, now)
            )
            assert created.status_code == 200, created.text
            job_id = created.json()["job_id"]

            async def interrupt():
                if interruption == "recall":
                    response = await client.post(
                        "/internal/v1/owner-authority/inputs/recall", json=notice(config, plan, now)
                    )
                elif interruption == "stop":
                    response = await stop(client, plan)
                else:
                    _, response = await admit(client, config, plan, now, 2)
                assert response.status_code in {200, 202}, response.text

            calls = []
            assert await worker(core, calls, hook=interrupt)
            receipt = await client.post("/internal/v1/memes/captions/" + job_id, json={})
            assert receipt.json()["result"] is None
            assert receipt.json()["usage"]["state"] == "SETTLED", receipt.text
            assert len(calls) == 1
            assert not core.store.rows("SELECT * FROM outbox")
            if interruption == "recall":
                row = core.store.one(
                    "SELECT asset_data,source_json,context_json FROM meme_captions"
                )
                assert row == {"asset_data": None, "source_json": "{}", "context_json": "{}"}

    run(scenario())


def test_unknown_usage_is_retained_and_same_asset_never_rebills(core, tmp_path, monkeypatch):
    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, _, client, _, _, now, item = values
            payload = body(core, event, item, now)
            created = await client.post("/internal/v1/memes/captions", json=payload)
            calls = []
            assert await worker(core, calls, tokens=False)
            receipt = await client.post(
                "/internal/v1/memes/captions/" + created.json()["job_id"], json={}
            )
            assert receipt.json()["usage"]["state"] == "UNKNOWN", receipt.text
            assert receipt.json()["usage"]["billed_micro_usd"] is None
            assert (
                core.store.one("SELECT billed_micro_usd FROM model_usage")["billed_micro_usd"] > 0
            )
            assert len(calls) == 1

    run(scenario())


def test_queued_offer_wait_is_bounded_and_empty_query_releases_chat(core, tmp_path, monkeypatch):
    # The fixture text has no intent; treat it as one that could carry an offer.
    monkeypatch.setattr(meme_core, "offer_possible", lambda text: True)

    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, accepted, client, _, _, _, _ = values
            core.store.execute(
                "UPDATE jobs SET state='QUEUED' WHERE job_id=%s", (accepted["job_id"],)
            )
            with Core(core.settings, clock=core.clock) as independent:
                assert independent.claim_job("chat") is None
                queried = await client.post(
                    "/internal/v1/memes/query",
                    json={"event_id": event["event_id"], "reply_revision": 1},
                )
                assert queried.status_code == 200, queried.text
                value = queried.json()
                assert value["query"]["intent"] is None and value["embedding"] is None
                assert value["query"]["text"] == "合成主人输入1"
                offered = await client.post(
                    "/internal/v1/memes/offers",
                    json={
                        **{
                            key: value[key]
                            for key in ("job_id", "event_id", "reply_revision", "query_sha256")
                        },
                        "offer": None,
                    },
                )
                assert offered.status_code == 200, offered.text
                assert independent.claim_job("chat")["job_id"] == accepted["job_id"]
                late = await client.post(
                    "/internal/v1/memes/offers",
                    json={
                        **{
                            key: value[key]
                            for key in ("job_id", "event_id", "reply_revision", "query_sha256")
                        },
                        "offer": None,
                    },
                )
                assert late.status_code == 409
            assert not core.store.rows("SELECT * FROM model_usage")

    run(scenario())


def test_offer_wait_expires_without_bridge_response(core, tmp_path, monkeypatch):
    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            _, accepted, _, _, _, _, _ = values
            core.store.execute(
                "UPDATE jobs SET state='QUEUED',created_at=%s WHERE job_id=%s",
                (after(core.clock(), -meme_core.OFFER_WAIT_SECONDS - 1), accepted["job_id"]),
            )
            with Core(core.settings, clock=core.clock) as independent:
                assert independent.claim_job("chat")["job_id"] == accepted["job_id"]

    run(scenario())


def test_new_source_cache_binding_keeps_original_erasure_lineage(core, tmp_path, monkeypatch):
    raw_event = meme_authority_fixture.raw_event
    monkeypatch.setattr(
        meme_authority_fixture,
        "raw_event",
        lambda *args, **kwargs: raw_event(*args, **{**kwargs, "text": "我想要抱抱"}),
    )
    monkeypatch.setattr(
        meme_core,
        "encoder",
        lambda *args, **kwargs: SimpleNamespace(query_embed=lambda text: [[1.0] + [0.0] * 511]),
    )

    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            first_event, _, client, config, plan, now, item = values
            original = await client.post(
                "/internal/v1/memes/captions", json=body(core, first_event, item, now)
            )
            calls = []
            assert await worker(core, calls)
            first = (
                await client.post(
                    "/internal/v1/memes/captions/" + original.json()["job_id"], json={}
                )
            ).json()
            assert first["state"] == "SUCCEEDED"
            second_event, accepted = await admit(client, config, plan, now, 2)
            assert accepted.status_code == 202, accepted.text
            core.store.execute(
                "UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (accepted.json()["job_id"],)
            )
            # Native discovery contract inherits this second input's actual revision.
            from companion import meme_service

            meme_service.submit(
                core.store,
                core.settings,
                core.clock,
                meme_service.DiscoveryRequest(
                    event_id=second_event["event_id"],
                    reply_revision=2,
                    intent="comfort",
                    provider="community",
                ),
            )
            second_claim = claim(core)
            reservation(core, second_claim)
            result(core, second_claim)
            second_payload = body(core, second_event, second_claim, now)
            second_payload["context"]["reply_revision"] = 2
            second = await client.post("/internal/v1/memes/captions", json=second_payload)
            assert second.status_code == 200, second.text
            assert second.json()["state"] == "SUCCEEDED" and second.json()["cache_hit"]
            assert second.json()["usage"] == first["usage"]
            assert len(core.store.rows("SELECT * FROM model_usage")) == len(calls) == 1
            current, accepted = await admit(client, config, plan, now, 3)
            queried = await client.post(
                "/internal/v1/memes/query",
                json={"event_id": current["event_id"], "reply_revision": 3},
            )
            assert queried.status_code == 200, queried.text
            value = queried.json()
            offer = {
                "offer_key": "meme:" + current["event_id"],
                "intent": "comfort",
                "candidates": [
                    {
                        "candidate_id": first["asset_sha256"],
                        "description": CAPTION["description"],
                        "tags": CAPTION["tags"],
                        "category": "like",
                    }
                ],
            }
            offered = await client.post(
                "/internal/v1/memes/offers",
                json={
                    **{
                        key: value[key]
                        for key in ("event_id", "reply_revision", "job_id", "query_sha256")
                    },
                    "offer": offer,
                },
            )
            assert offered.status_code == 200, offered.text
            sources = core.store.rows(
                'SELECT event_id FROM job_sources WHERE job_id=%s', (accepted.json()["job_id"],)
            )
            assert {row["event_id"] for row in sources} == {
                first_event["event_id"],
                second_event["event_id"],
                current["event_id"],
            }
            recalled = await client.post(
                "/internal/v1/owner-authority/inputs/recall", json=notice(config, plan, now, 1)
            )
            assert recalled.status_code == 200, recalled.text
            assert not core.store.one(
                'SELECT 1 FROM runtime_meta WHERE key=%s', (meme_core.OFFER + current["event_id"],)
            )
            assert all(
                row["result_json"] is None for row in core.store.rows("SELECT * FROM meme_captions")
            )
            assert (
                core.store.one(
                    'SELECT redacted FROM events WHERE event_id=%s', (second_event["event_id"],)
                )["redacted"]
                == 0
            )

    run(scenario())


def test_crash_after_reservation_recovers_original_receipt_without_resubmit(
    core, tmp_path, monkeypatch
):
    class Crash(BaseException):
        pass

    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, _, client, _, _, now, item = values
            created = await client.post(
                "/internal/v1/memes/captions", json=body(core, event, item, now)
            )
            reserve = budget.reserve

            def crash(*args, **kwargs):
                reserve(*args, **kwargs)
                raise Crash()

            monkeypatch.setattr(budget, "reserve", crash)
            calls = []
            with pytest.raises(Crash):
                await worker(core, calls)
            row = core.store.one("SELECT * FROM meme_captions")
            assert (
                row["usage_request_id"]
                == core.store.one("SELECT request_id FROM model_usage")["request_id"]
            )
            core.clock.advance(core.settings.lease_seconds + 1)
            monkeypatch.setattr(budget, "reserve", reserve)
            assert not await worker(core, calls)
            receipt = (
                await client.post(
                    "/internal/v1/memes/captions/" + created.json()["job_id"], json={}
                )
            ).json()
            assert receipt["state"] == "UNKNOWN" and receipt["usage"]["state"] == "UNKNOWN"
            assert not calls and len(core.store.rows("SELECT * FROM model_usage")) == 1

    run(scenario())


def test_local_query_embedding_cannot_block_new_input_or_publish_stale_offer(
    core,
    tmp_path,
    monkeypatch,
):
    raw_event = meme_authority_fixture.raw_event
    monkeypatch.setattr(
        meme_authority_fixture,
        "raw_event",
        lambda *args, **kwargs: raw_event(*args, **{**kwargs, "text": "我想要抱抱"}),
    )

    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, accepted, client, config, plan, now, _ = values
            core.store.execute(
                "UPDATE jobs SET state='QUEUED' WHERE job_id=%s", (accepted["job_id"],)
            )
            loop = asyncio.get_running_loop()
            started, release = asyncio.Event(), threading.Event()

            def slow(self, text, *, query=False):
                assert query and text == "我想要抱抱"
                loop.call_soon_threadsafe(started.set)
                assert release.wait(5)
                return meme_core.EmbeddingBatch(
                    model_version=meme_core.SPEC.model_version,
                    dimensions=512,
                    vectors=((1.0, *([0.0] * 511)),),
                    local=True,
                )

            monkeypatch.setattr(meme_core.MemeCaptionService, "_embedding", slow)
            pending = asyncio.create_task(
                client.post(
                    "/internal/v1/memes/query",
                    json={"event_id": event["event_id"], "reply_revision": 1},
                )
            )
            try:
                await asyncio.wait_for(started.wait(), 2)
                _, incoming = await asyncio.wait_for(admit(client, config, plan, now, 2), 2)
                assert incoming.status_code == 202, incoming.text
            finally:
                release.set()
            stale = await pending
            assert stale.status_code == 409, stale.text
            assert not core.store.one(
                'SELECT 1 FROM runtime_meta WHERE key=%s', (meme_core.QUERY + event["event_id"],)
            )
            assert not core.store.rows("SELECT * FROM model_usage")

    run(scenario())


def test_stale_unpaid_caption_does_not_block_current_source(core, tmp_path, monkeypatch):
    from companion import meme_service

    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, _, client, config, plan, now, item = values
            original = await client.post(
                "/internal/v1/memes/captions", json=body(core, event, item, now)
            )
            assert original.status_code == 200, original.text
            current, accepted = await admit(client, config, plan, now, 2)
            assert accepted.status_code == 202, accepted.text
            core.store.execute(
                "UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (accepted.json()["job_id"],)
            )
            meme_service.submit(
                core.store,
                core.settings,
                core.clock,
                meme_service.DiscoveryRequest(
                    event_id=current["event_id"],
                    reply_revision=2,
                    intent="comfort",
                    provider="community",
                ),
            )
            discovered = claim(core)
            reservation(core, discovered)
            result(core, discovered)
            payload = body(core, current, discovered, now)
            payload["context"]["reply_revision"] = 2
            created = await client.post("/internal/v1/memes/captions", json=payload)
            assert created.status_code == 200, created.text
            calls = []
            for _ in range(3):
                if not await worker(core, calls):
                    break
            receipt = await client.post(
                "/internal/v1/memes/captions/" + created.json()["job_id"], json={}
            )
            assert receipt.json()["state"] == "SUCCEEDED", receipt.text
            assert not receipt.json()["cache_hit"]
            assert len(calls) == len(core.store.rows("SELECT * FROM model_usage")) == 1

    run(scenario())


@pytest.mark.parametrize("bind_before_failure", [False, True])
def test_pre_admission_failure_cannot_poison_new_caption_binding(
    core, tmp_path, monkeypatch, bind_before_failure
):
    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, _, client, _, _, now, item = values
            payload = body(core, event, item, now)
            original = await client.post("/internal/v1/memes/captions", json=payload)
            assert original.status_code == 200, original.text
            payload["source_evidence"]["usage_basis"] += "; renewed source proof"
            payload["asset"]["source_evidence_sha256"] = digest(payload["source_evidence"])
            if bind_before_failure:
                created = await client.post("/internal/v1/memes/captions", json=payload)
                assert created.status_code == 200, created.text
                assert core.store.one(
                    'SELECT reuse_job_id FROM meme_captions WHERE job_id=%s',
                    (created.json()["job_id"],),
                )["reuse_job_id"] == original.json()["job_id"]
                payload["source_evidence"]["usage_basis"] += "; third source proof"
                payload["asset"]["source_evidence_sha256"] = digest(payload["source_evidence"])
                third = await client.post("/internal/v1/memes/captions", json=payload)
                assert third.status_code == 200, third.text

            def rejected(*args, **kwargs):
                raise CoreError("MODEL_BUDGET_EXCEEDED", 429)

            calls = []
            with monkeypatch.context() as patch:
                patch.setattr(budget, "reserve", rejected)
                assert await worker(core, calls)
            assert not calls and not core.store.rows("SELECT * FROM model_usage")
            assert core.store.one(
                'SELECT usage_request_id FROM meme_captions WHERE job_id=%s',
                (original.json()["job_id"],),
            )["usage_request_id"]
            if not bind_before_failure:
                created = await client.post("/internal/v1/memes/captions", json=payload)
                assert created.status_code == 200, created.text
            assert await worker(core, calls)
            receipt = await client.post(
                "/internal/v1/memes/captions/" + created.json()["job_id"], json={}
            )
            assert receipt.json()["state"] == "SUCCEEDED", receipt.text
            assert not receipt.json()["cache_hit"]
            if bind_before_failure:
                assert await worker(core, calls)
                reused = await client.post(
                    "/internal/v1/memes/captions/" + third.json()["job_id"], json={}
                )
                assert reused.json()["state"] == "SUCCEEDED", reused.text
                assert reused.json()["cache_hit"]
                assert reused.json()["usage"] == receipt.json()["usage"]
            assert len(calls) == len(core.store.rows("SELECT * FROM model_usage")) == 1

    run(scenario())


def test_caption_http_accepts_bounded_original_media_above_default_body_limit(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, _, client, _, _, now, item = values
            output = io.BytesIO()
            Image.frombytes("RGB", (256, 256), random.Random(42).randbytes(256 * 256 * 3)).save(
                output, format="PNG"
            )
            payload = body(core, event, item, now, data=output.getvalue())
            assert len(json.dumps(payload).encode()) > 131072
            created = await client.post("/internal/v1/memes/captions", json=payload)
            assert created.status_code == 200, created.text
            oversized = await client.post(
                "/internal/v1/memes/captions", content=b" " * (6 * 1024 * 1024 + 1)
            )
            assert oversized.status_code == 413 and oversized.json()["error"] == "BODY_TOO_LARGE"
            calls = []
            assert await worker(core, calls)
            receipt = await client.post(
                "/internal/v1/memes/captions/" + created.json()["job_id"], json={}
            )
            assert receipt.json()["state"] == "SUCCEEDED", receipt.text
            assert len(calls) == len(core.store.rows("SELECT * FROM model_usage")) == 1

    run(scenario())


def test_text_without_a_meme_intent_is_claimed_without_waiting(core, tmp_path, monkeypatch):
    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            _, accepted, _, _, _, _, _ = values
            core.store.execute(
                "UPDATE jobs SET state='QUEUED' WHERE job_id=%s", (accepted["job_id"],)
            )
            with Core(core.settings, clock=core.clock) as independent:
                assert independent.claim_job("chat")["job_id"] == accepted["job_id"]

    run(scenario())
    assert meme_core.offer_possible("好可爱的猫") and not meme_core.offer_possible("在吗")

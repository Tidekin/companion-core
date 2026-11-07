"""Signed owner cards use the existing bounded Reader and chat context path."""

import json

import httpx
import pytest

from companion import owner_authority, owner_input
from companion.contracts import CoreError
from companion.model import ModelClient
from companion.owner_authority_protocol import AuthorityStop
from companion.source_grants import ReaderReadValidation
from companion.store import encode
from tests.conftest import run
from tests.support.live_card_content import TOKEN, URL, captured, options, result
from tests.support.live_image_input import options as image_options
from tests.support.live_image_input import png
from tests.support.native_consumer import source_window
from tests.support.onebot_events import raw_event
from tests.support.onebot_images import image_segment
from tests.support.qq_rich_input import card


def test_authenticated_card_reader_success_reaches_separate_chat_context(core, tmp_path):
    calls = []

    def model(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "文章比较了两款耳机的佩戴体验。"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    async def scenario():
        async with captured(core, tmp_path) as (_, client, event, sends), httpx.AsyncClient(
            transport=httpx.MockTransport(model),
        ) as model_http:
            core.model = ModelClient(core.settings, model_http)
            auth = {"Authorization": "Bearer " + TOKEN}
            prefix = "/internal/v3/readers/reader"
            assert (await client.post(prefix + "/lease", json={})).status_code == 401
            assert not await core.run_once()
            assert not calls
            response = await client.post(prefix + "/lease", json={}, headers=auth)
            assert response.status_code == 200, response.text
            work = response.json()["item"]
            assert work["url"] == URL and work["operation"] == "read"
            checked = await client.post(prefix + "/" + work["request_id"] + "/validate", json={
                "lease_token": work["lease_token"], "source_grant": None,
            }, headers=auth)
            assert checked.status_code == 200 and checked.json() == {"valid": True}
            finished = await client.post(prefix + "/" + work["request_id"] + "/result",
                                        json=result(work, core).model_dump(mode="json"),
                                        headers=auth)
            assert finished.status_code == 200, finished.text
            assert await core.run_once()
            assert len(calls) == 1
            context = json.dumps(calls[0]["messages"], ensure_ascii=False)
            assert "作者比较两款耳机的佩戴体验" in context
            observation = finished.json()["event_ids"][0]
            references = [json.loads(row["content"])["shared_content"]
                for row in calls[0]["messages"] if row["role"] == "user"
                and row["content"].startswith('{"shared_content":')]
            assert len(references) == 1
            assert any(item["url"] == URL and item["trust"] == "external_untrusted"
                       for item in references[0])
            assert "QQ引用资料" in context
            assert next(row["content"] for row in reversed(calls[0]["messages"])
                if row["role"] == "user") == "请总结这份分享"
            assert calls[0]["messages"][-1]["role"] == "tool"
            reading = json.loads(calls[0]["messages"][-1]["content"])
            assert {event["event_id"], observation} <= set(reading["source_event_ids"])
            assert any(item["event_id"] == observation for item in reading["items"])
            assert core.store.rows(
                "SELECT source_event_id FROM event_dependencies WHERE event_id=?", (observation,),
            ) == [{"source_event_id": event["event_id"]}]
            assert not sends

    run(scenario())


@pytest.mark.parametrize("boundary", ["lease", "validate", "complete"])
def test_stopped_window_fences_reader_work_at_each_boundary(core, tmp_path, boundary):
    async def scenario():
        async with captured(core, tmp_path) as (window, _, _, _):
            work = None if boundary == "lease" else core.reading.lease("reader")
            owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                operation_id="stop-card-boundary", authority=window.runtime.authority))
            if boundary == "lease":
                assert core.reading.lease("reader") is None
                assert not core.store.one("SELECT 1 FROM observations")
                return
            if boundary == "validate":
                with pytest.raises(CoreError):
                    core.reading.validate_read("reader", work["request_id"],
                        ReaderReadValidation(lease_token=work["lease_token"]))
            else:
                assert core.reading.complete("reader", work["request_id"], result(work, core)) == {
                    "state": "CANCELLED", "event_ids": [], "duplicate": False}
            assert not core.store.one("SELECT 1 FROM observations")

    run(scenario())


def test_card_read_waits_for_same_input_image_fetch_and_vision_before_claiming(core, tmp_path):
    combined, links = image_options(), options()
    combined["link_scope"] = "owner_links_v1"
    combined["core_options"] = {
        **links["core_options"], **combined["core_options"],
        "observation_adapters": {
            **combined["core_options"]["observation_adapters"],
            **links["core_options"]["observation_adapters"],
        },
    }
    phases = []

    async def source(ws):
        await ws.send(json.dumps(raw_event(time=int(core.clock.now.timestamp()),
            message=[card(url=URL), image_segment()])))
        await ws.wait_closed()

    def not_leased():
        assert core.reading.lease("reader") is None
        assert not core.store.one("SELECT * FROM exploration_requests")

    def cdn(request):
        phases.append("FETCHING")
        not_leased()
        return httpx.Response(200, content=png(), headers={"Content-Type": "image/png"})

    def model(request):
        phases.append("RUNNING")
        assert core.store.one("SELECT state FROM model_usage")["state"] == "RESERVED"
        not_leased()
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "图中为蓝色区域", "tool_calls": [
                {"id": "read-card", "type": "function", "function": {
                    "name": "read_content", "arguments": json.dumps({"url": URL})}}]},
                "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    async def resolve(host, port):
        return ["1.1.1.1"]

    async def scenario():
        async with source_window(
            core, tmp_path, source, scope_options=combined,
        ) as (window, client, protocol, _, _), httpx.AsyncClient(
            transport=httpx.MockTransport(model),
        ) as model_http:
            core.model = ModelClient(core.settings, model_http)
            core.content.options = core.settings.content
            await window.capture_once(client, protocol)
            from companion_onebot_bridge import pending_media
            from tests.sdk_reader_fixture import worker_for

            assert await window.runtime.flush_one(client)
            assert core.claim_job("wait-for-card-image") is None
            row = pending_media._execute(window.runtime,
                "SELECT * FROM automatic_media").fetchone()
            await pending_media.MediaPump(window.runtime, client,
                image_transport=httpx.MockTransport(cdn), image_resolver=resolve).execute(row)
            async with worker_for(core, model) as worker:
                assert await worker.run_once("select-image-card")
            assert phases == ["FETCHING", "RUNNING"]
            assert core.store.one(
                "SELECT state FROM content_items WHERE kind='qq_image'",
            )["state"] == "SUCCEEDED"
            work = core.reading.lease("reader")
            assert work and work["url"] == URL

    run(scenario())


@pytest.mark.parametrize("drift", ["body", "receipt", "signature", "target", "source", "delete"])
def test_card_source_drift_or_erasure_prevents_actual_read_and_result(core, tmp_path, drift):
    async def scenario():
        async with captured(core, tmp_path) as (_, _, event, _):
            work = core.reading.lease("reader")
            if drift == "body":
                body = json.loads(event["body_json"])
                body["rich"]["segments"][0]["fields"][0]["value"] = "changed"
                core.store.execute("UPDATE events SET body_json=? WHERE event_id=?",
                                   (encode(body), event["event_id"]))
            elif drift in {"receipt", "signature"}:
                key = owner_input.INPUT + event["event_id"]
                value = json.loads(core.store.one(
                    "SELECT value FROM runtime_meta WHERE key=?", (key,),
                )["value"])
                if drift == "receipt":
                    value["capture"]["input_sha256"] = "0" * 64
                else:
                    value["capture"]["signature"] = "0" * 128
                core.store.execute(
                    "UPDATE runtime_meta SET value=? WHERE key=?", (encode(value), key),
                )
            elif drift == "target":
                core.store.execute("UPDATE exploration_requests SET url=?", (URL + "7",))
            elif drift == "source":
                core.store.execute("UPDATE events SET source='observation.heybox' WHERE event_id=?",
                                   (event["event_id"],))
            else:
                with core.store.transaction():
                    core.ledger.expire_events(core.store, [event["event_id"]], core.clock())
            with pytest.raises(CoreError):
                core.reading.validate_read("reader", work["request_id"],
                    ReaderReadValidation(lease_token=work["lease_token"]))
            assert core.reading.complete("reader", work["request_id"], result(work, core)) == {
                "state": "CANCELLED", "event_ids": [], "duplicate": False}
            assert not core.store.one("SELECT 1 FROM observations")
            assert not core.model.calls

    run(scenario())


@pytest.mark.parametrize("kind", ["text", "reply"])
def test_only_current_json_card_urls_can_queue_or_lease_bounded_reads(core, tmp_path, kind):
    message = [{"type": "text", "data": {"text": URL}}] if kind == "text" else [
        {"type": "reply", "data": {"id": "43", "message": URL}},
    ]

    async def scenario():
        async with captured(core, tmp_path, message) as (_, _, event, _):
            from tests.sdk_reader_fixture import Provider, answer, call, worker_for

            items = core.store.rows("SELECT target_url FROM content_items")
            assert items == ([{"target_url": URL}] if kind == "text" else [])
            assert not core.store.one("SELECT 1 FROM exploration_requests")
            requested = URL + "7" if kind == "text" else URL
            provider = Provider(answer(calls=[call("foreign-read", "read_content",
                {"url": requested})]), answer("没有收到这份正文。"))
            async with worker_for(core, provider) as worker:
                assert await worker.run_once("reject-unshared-url")
            assert "OWNER_LINK_REQUIRED" in encode(provider.requests[-1])
            assert core.reading.lease("reader") is None
            assert not core.store.one("SELECT 1 FROM exploration_requests")
            assert not core.store.one("SELECT 1 FROM observations")

    run(scenario())

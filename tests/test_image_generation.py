"""Model-requested pictures: one priced tool call, Outbox attachment and leased Bridge read."""

import base64
import io
import json
from contextlib import closing

import httpx
import pytest
from PIL import Image
from pydantic import SecretStr, ValidationError

from companion import image_generation
from companion.contracts import CoreError
from companion.media_config import ImageGenerationConfig, MediaEndpoint
from companion.media_provider import HTTPImageGenerator
from companion.store import Store
from companion_onebot_bridge.generated_image_http import read_image
from companion_onebot_bridge.outbound import validate_item
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, call, worker_for
from tests.test_native_image_pipeline import admit_image
from tests.test_prepare_owner_input import native_spool, window

URL = "https://images.example.test/v1/images/generations"


def png(color="orange", size=(16, 12)):
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, format="PNG")
    return stream.getvalue()


def config(**changes):
    values = {
        "enabled": True,
        "endpoint": MediaEndpoint(url=URL, key=SecretStr("synthetic-image-provider-key"),
                                  model="gpt-image-1", max_request_micro_usd=40000),
        "daily_micro_usd": 200000,
        **changes,
    }
    return ImageGenerationConfig(**values)


def image_reply(data):
    return httpx.Response(200, json={"created": 1, "data": [
        {"b64_json": base64.b64encode(data).decode()}]})


def test_maintenance_preserves_bytes_between_dispatch_and_result_commit(
    core, tmp_path, monkeypatch,
):
    original_write, observed = image_generation._write, []

    def write(store, image_id, data):
        root = image_generation.directory(store)
        root.mkdir(mode=0o700, exist_ok=True)
        partial = root / (image_id + ".tmp")
        partial.write_bytes(data[:10])
        with closing(Store(store.path)) as other:
            image_generation.cleanup(other, core.clock())
            assert partial.exists()
            partial.unlink()
            original_write(store, image_id, data)
            image_generation.cleanup(other, core.clock())
            assert (root / (image_id + ".img")).exists()
        observed.append(image_id)

    async def scenario():
        async with window(core, tmp_path, expression=True) as (client, config_, plan):
            core.settings.image_generation = config()
            await owner_text(core, client, config_, plan, tmp_path, "合成生图请求")
            selected = Provider(draw(), answer("画好啦，给你。"))

            def provider(request):
                return (image_reply(png()) if request.url.host == "images.example.test"
                        else selected(request))

            async with worker_for(core, provider) as worker:
                worker.image_transport = httpx.MockTransport(provider)
                assert await worker.run_once("generated-image-maintenance")
            assert len(observed) == 1
            assert core.store.one("SELECT state FROM generated_images")["state"] == "SUCCEEDED"

    monkeypatch.setattr(image_generation, "_write", write)
    run(scenario())


def draw(prompt="一只在窗边晒太阳的橘猫", size="1024x1024", call_id="draw-1"):
    return answer(calls=[call(call_id, "generate_image", {"prompt": prompt, "size": size})])


async def owner_text(core, client, config_, plan, tmp_path, text, number=42):
    event, _ = await admit_image(core, client, config_, plan, tmp_path, number=number,
                                 message=[{"type": "text", "data": {"text": text}}])
    return event


def test_config_requires_priced_https_provider():
    assert ImageGenerationConfig().enabled is False
    with pytest.raises(ValidationError):
        ImageGenerationConfig(enabled=True)
    with pytest.raises(ValidationError):
        config(daily_micro_usd=100)
    with pytest.raises(ValidationError):
        config(endpoint=MediaEndpoint(url="http://images.example.test/v1", model="m",
                                      key=SecretStr("k"), max_request_micro_usd=1))


@pytest.mark.parametrize("case", ["redirect", "url_only", "too_large", "two", "lost"])
def test_adapter_accepts_only_one_inline_bounded_image(case):
    def handler(request):
        if case == "redirect":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/steal"})
        if case == "url_only":
            return httpx.Response(200, json={"data": [{"url": "https://cdn.example/x.png"}]})
        if case == "too_large":
            return image_reply(b"x" * 100_000)
        if case == "two":
            return httpx.Response(200, json={"data": [{"b64_json": "AA=="}] * 2})
        raise httpx.ReadTimeout("synthetic lost answer", request=request)

    generator = HTTPImageGenerator(config(max_output_bytes=1024), httpx.MockTransport(handler))
    expected = {"redirect": "IMAGE_PROVIDER_REJECTED",
                "url_only": "INVALID_IMAGE_PROVIDER_RESPONSE",
                "too_large": "IMAGE_PROVIDER_RESPONSE_TOO_LARGE",
                "two": "INVALID_IMAGE_PROVIDER_RESPONSE", "lost": "IMAGE_PROVIDER_UNCERTAIN"}
    with pytest.raises(CoreError) as error:
        run(generator.generate("cat", "1024x1024"))
    assert error.value.code == expected[case]


def test_adapter_request_shape_depends_on_model_family():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer synthetic-image-provider-key"
        return image_reply(png())

    transport = httpx.MockTransport(handler)
    assert run(HTTPImageGenerator(config(), transport).generate("cat", "1024x1024")) == png()
    other = config(endpoint=MediaEndpoint(url=URL, key=SecretStr("synthetic-image-provider-key"),
                                          model="dall-e-3", max_request_micro_usd=40000))
    run(HTTPImageGenerator(other, transport).generate("cat", "1024x1024"))
    assert "response_format" not in bodies[0]
    assert bodies[1]["response_format"] == "b64_json"
    assert bodies[0] == {"model": "gpt-image-1", "prompt": "cat", "n": 1, "size": "1024x1024"}


def test_generated_image_reaches_reply_lease_bridge_and_erasure(core, tmp_path):
    data = png()

    async def scenario():
        async with window(core, tmp_path, expression=True) as (client, config_, plan):
            core.settings.image_generation = config()
            event = await owner_text(core, client, config_, plan, tmp_path,
                                     "帮我画一只在窗边晒太阳的橘猫")
            provider, images = Provider(draw(), answer("画好啦，给你。")), []

            def respond(request):
                if request.url.host == "images.example.test":
                    images.append(json.loads(request.content))
                    return image_reply(data)
                return provider(request)

            async with worker_for(core, respond) as worker:
                worker.image_transport = httpx.MockTransport(respond)
                assert await worker.run_once("generated-image")
            assert "generate_image" in json.dumps(provider.requests[0]["tools"])
            assert images == [{"model": "gpt-image-1", "prompt": "一只在窗边晒太阳的橘猫",
                               "n": 1, "size": "1024x1024"}]
            assert [(row["scenario"], row["state"]) for row in core.store.rows(
                "SELECT scenario,state FROM model_usage ORDER BY rowid")] == [
                ("chat", "SETTLED"), ("image_generation", "SETTLED"), ("chat", "SETTLED")]
            generated = core.store.one("SELECT * FROM generated_images")
            assert generated["state"] == "SUCCEEDED" and generated["event_id"] == event["event_id"]
            outbox = core.store.rows("SELECT * FROM outbox")
            assert len(outbox) == 1 and generated["message_id"] == outbox[0]["message_id"]
            content = json.loads(outbox[0]["content_json"])
            assert content["text"] == "画好啦，给你。"
            assert content["generated_image"] == image_generation.reference(generated)
            assert "data" not in json.dumps(content)

            leased = await client.post("/internal/v1/deliveries/lease", json={
                "worker_id": "generated-image-test", "authority": plan.authority, "limit": 1})
            assert leased.status_code == 200, leased.text
            item = leased.json()["items"][0]
            validate_item(item, config_, companion=True, generated_image_scope=True)
            with pytest.raises(ValueError, match="GENERATED_IMAGE_SCOPE_REQUIRED"):
                validate_item(item, config_, companion=True)
            spool, runtime = native_spool(core, tmp_path, config_, plan)
            try:
                from companion_onebot_bridge.network import private_http_client

                async with private_http_client(config_.core_url, config_.bridge_token) as bridge:
                    prepared = await read_image(runtime, bridge, item, check=lambda: None)
                assert prepared.data == data and prepared.mime_type == "image/png"
                path = ("/internal/v1/generated-images/deliveries/" + item["message_id"]
                        + "/asset")
                body = {"authority": plan.authority, "event_id": item["origin_event_id"],
                        "reply_revision": item["reply_revision"],
                        "lease_token": item["lease_token"],
                        "content_sha256": item["content_sha256"]}
                denied = await client.post(path, json={**body, "lease_token": "lease_" + "b" * 32})
                assert denied.status_code == 409 and "data_base64" not in denied.text
                with core.store.transaction():
                    core.ledger.expire_events(core.store, [event["event_id"]], core.clock())
                erased = await client.post(path, json=body)
                assert erased.status_code in {403, 409, 410} and "data_base64" not in erased.text
                assert core.store.one("SELECT state,sha256 FROM generated_images") == {
                    "state": "ERASED", "sha256": None}
                image_generation.cleanup(core.store, core.clock())
                assert not list(image_generation.directory(core.store).iterdir())
            finally:
                spool.close()
    run(scenario())


def test_unknown_image_outcome_keeps_charge_sends_text_and_never_repeats(core, tmp_path):
    async def scenario():
        async with window(core, tmp_path, expression=True) as (client, config_, plan):
            core.settings.image_generation = config()
            await owner_text(core, client, config_, plan, tmp_path, "画一张星空")
            provider, images = Provider(draw("星空"), answer("这次没画出来，换个说法再试？")), []

            def respond(request):
                if request.url.host == "images.example.test":
                    images.append(request)
                    raise httpx.ReadTimeout("synthetic lost image answer", request=request)
                return provider(request)

            async with worker_for(core, respond) as worker:
                worker.image_transport = httpx.MockTransport(respond)
                assert await worker.run_once("generated-image-unknown")
            assert len(images) == 1
            usage = core.store.rows("SELECT scenario,state FROM model_usage ORDER BY rowid")
            assert ("image_generation", "UNKNOWN") in [(r["scenario"], r["state"]) for r in usage]
            assert core.store.one("SELECT state FROM generated_images")["state"] == "UNKNOWN"
            tool = core.store.one("SELECT * FROM agent_tool_calls")
            assert tool["state"] == "RETURNED" and "UNKNOWN" in tool["result_json"]
            outbox = core.store.rows("SELECT content_json FROM outbox")
            assert len(outbox) == 1 and "generated_image" not in outbox[0]["content_json"]
            before = core.store.rows("SELECT * FROM model_usage")
            async with worker_for(core, respond) as restarted:
                restarted.image_transport = httpx.MockTransport(respond)
                await restarted.run_once("generated-image-no-replay")
            assert len(images) == 1
            assert core.store.rows("SELECT * FROM model_usage") == before
    run(scenario())


@pytest.mark.parametrize("outcome", ["ack", "expired", "lost_ack"])
def test_generated_image_native_delivery_and_uncertainty(core, tmp_path, outcome):
    from companion_onebot_bridge.network import private_http_client
    from companion_onebot_bridge.outbound import deliver_tick
    from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
    from tests.support.onebot import provider as onebot_provider

    async def scenario():
        async with window(core, tmp_path, expression=True) as (client, config_, plan):
            core.settings.image_generation = config()
            await owner_text(core, client, config_, plan, tmp_path, "画一只猫")
            data = png()
            model = Provider(draw(), answer("画好啦，给你。"))
            async with worker_for(core, model) as worker:
                worker.image_transport = httpx.MockTransport(lambda request: image_reply(data))
                assert await worker.run_once("generated-image-native-send")
            usage = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
            if outcome == "expired":
                core.store.execute("UPDATE generated_images SET expires_at=?", (core.clock(),))

            spool, runtime = native_spool(core, tmp_path, config_, plan)
            sends = []

            def transport(request):
                lost = httpx.ReadTimeout("synthetic ACK loss", request=request)
                return onebot_provider(request, sends, lost if outcome == "lost_ack" else None)

            try:
                async with private_http_client(config_.core_url, config_.bridge_token) as bridge, (
                    private_http_client(config_.onebot_http_url, config_.onebot_token,
                    transport=httpx.MockTransport(transport),
                )) as protocol:
                    guard = OwnerSendGuard(runtime, bridge)
                    assert await deliver_tick(bridge, OwnerOneBot(guard, protocol), spool,
                                              guard=guard)
                assert len(sends) == 1
                segments = sends[0]["message"]
                assert segments[0] == {"type": "text", "data": {"text": "画好啦，给你。"}}
                if outcome == "expired":
                    assert len(segments) == 1
                else:
                    assert segments[1] == {"type": "image", "data": {
                        "file": "base64://" + base64.b64encode(data).decode()}}
                outbox = core.store.one("SELECT state,attempt_count FROM outbox")
                assert outbox == {"state": "DELIVERY_UNCERTAIN" if outcome == "lost_ack"
                                  else "SENT", "attempt_count": 1}
                assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid") == usage
                if outcome == "lost_ack":
                    assert runtime.row()["state"] == "STOPPED"
            finally:
                spool.close()
    run(scenario())


@pytest.mark.parametrize("case", ["serious", "size", "daily", "twice"])
def test_refusals_never_reach_the_provider(core, tmp_path, case):
    async def scenario():
        async with window(core, tmp_path, expression=True) as (client, config_, plan):
            core.settings.image_generation = config(
                max_daily_images=0 if case == "daily" else 10)
            text = "我很难过，帮我画张图" if case == "serious" else "画一只猫"
            await owner_text(core, client, config_, plan, tmp_path, text)
            first = draw(size="1536x1024" if case == "size" else "1024x1024")
            replies = [first]
            if case == "twice":
                replies.append(draw("第二只猫", call_id="draw-2"))
            provider, images = Provider(*replies, answer("用文字回你。")), []

            def respond(request):
                if request.url.host == "images.example.test":
                    images.append(request)
                    return image_reply(png())
                return provider(request)

            async with worker_for(core, respond) as worker:
                worker.image_transport = httpx.MockTransport(respond)
                assert await worker.run_once("generated-image-refusal")
            assert len(images) == (1 if case == "twice" else 0)
            results = [row["result_json"] for row in core.store.rows(
                "SELECT result_json FROM agent_tool_calls ORDER BY rowid")]
            if case == "size":
                # The tool schema lists configured sizes only; the SDK rejects the call.
                assert not results and "1024x1024" in json.dumps(provider.requests[0]["tools"])
            else:
                code = {"serious": "IMAGE_CONTEXT_SERIOUS",
                        "daily": "IMAGE_GENERATION_DAILY_LIMIT",
                        "twice": "IMAGE_GENERATION_ONCE_PER_REPLY"}[case]
                assert code in results[-1]
            outbox = core.store.rows("SELECT content_json FROM outbox")
            assert len(outbox) == 1
            assert ("generated_image" in outbox[0]["content_json"]) == (case == "twice")
    run(scenario())


@pytest.mark.parametrize("case", ["no_expression", "real_transport", "disabled"])
def test_tool_is_hidden_without_grant_synthetic_transport_or_switch(core, tmp_path, case):
    async def scenario():
        async with window(core, tmp_path, expression=case != "no_expression") as (
                client, config_, plan):
            core.settings.image_generation = (ImageGenerationConfig() if case == "disabled"
                                              else config())
            await owner_text(core, client, config_, plan, tmp_path, "画一只猫")
            provider = Provider(answer("我先用文字描述一下。"))
            async with worker_for(core, provider) as worker:
                if case != "real_transport":
                    worker.image_transport = httpx.MockTransport(provider)
                assert await worker.run_once("generated-image-hidden")
            assert "generate_image" not in json.dumps(provider.requests[0].get("tools", []))
    run(scenario())

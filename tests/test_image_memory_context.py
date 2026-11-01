"""Native image clues → scoped retrieval → approved facts and originals in final vision."""

import base64
import io
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from PIL import Image

from companion import image_spool, live_text, paid_budget
from companion.model import ModelClient
from companion.store import Store, encode
from tests.conftest import run
from tests.support.failure_notice import assert_failure_notice
from tests.support.native_image_memory import native_image_memory
from tests.test_live_image_input import options
from tests.test_memory_commands import fact
from tests.test_onebot_capture import capture_approval, source_window
from tests.test_onebot_direct import raw_event
from tests.test_onebot_images import image_segment

PREFERENCE = "我挑选耳机优先考虑轻便和隔音。"
CUES = "图片1：红色轻便头戴耳机；图片2：蓝色头戴耳机。检索主题：耳机、轻便、隔音。"
QUESTION = "结合我以前的需求，这两张图里的哪一个更合适？"


@asynccontextmanager
async def image_memory_window(
    core, tmp_path, *, mutation=None, no_candidates=False, model_attempts=None,
    scope_options=None, reply=None,
):
    memory_id = fact(core, 900, PREFERENCE)
    outsider = fact(core, 901, "未批准的秘密偏好：紫色键盘。")
    pin = live_text.memory_fact(core.store, core.settings, core.clock, memory_id)
    opts = options()
    opts.update(memory_scope="approved_facts", memory_facts=[pin], memory_approval_sha256="f" * 64)
    opts["core_options"].update(memory_enabled=True)
    opts["paid_options"].update(per_request_calls=3)
    if model_attempts is not None:
        opts["max_model_attempts"] = model_attempts
        opts["paid_options"].update(window_calls=model_attempts, daily_calls=model_attempts)
    opts["paid_options"]["image_input"] = opts["paid_options"]["image_input"].model_copy(
        update={"max_images_per_request": 2},
    )
    if scope_options:
        opts["core_options"].update(scope_options.get("core_options", {}))
        opts.update({key: value for key, value in scope_options.items() if key != "core_options"})
    calls, downloads, queries = [], [], []
    originals = []
    for color in ("red", "blue"):
        stream = io.BytesIO()
        Image.new("RGB", (3, 2), color).save(stream, format="PNG")
        originals.append(stream.getvalue())
    original_scores = core.memory.index.scores_async

    async def scores(query, rows, **kwargs):
        queries.append((query, [row["memory_id"] for row in rows]))
        if no_candidates:
            return {row["memory_id"]: -1 for row in rows}
        return await original_scores(query, rows, **kwargs)

    core.memory.index.scores_async = scores

    number = 0

    async def source(ws):
        nonlocal number
        number += 1
        await ws.send(json.dumps(raw_event(number, message=[
            {"type": "text", "data": {"text": QUESTION}},
            image_segment("https://gchat.qpic.cn/red.png"),
            image_segment("https://gchat.qpic.cn/blue.png"),
        ])))
        await ws.wait_closed()

    async def resolve(host, port):
        return ["1.1.1.1"]

    def cdn(request):
        downloads.append(request)
        return httpx.Response(
            200, content=originals[len(downloads) - 1], headers={"Content-Type": "image/png"},
        )

    def model(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["messages"][0]["role"] == "user":
            content = CUES
        elif payload.get("response_format"):
            review = json.loads(payload["messages"][-1]["content"])
            content = json.dumps({"relevant": [item["ref"] for item in review["memories"]]})
            if mutation == "unknown_review":
                raise httpx.ReadTimeout("synthetic review timeout", request=request)
            if mutation == "erased_current":
                event_id = core.store.one(
                    "SELECT owner_event_id FROM content_items",
                )["owner_event_id"]
                with core.store.transaction():
                    core.ledger.expire_events(core.store, [event_id], core.clock())
        else:
            content = reply or (
                "图1可见红色。按你以前明确提到的轻便需求，我更倾向图1；隔音仅凭图片无法确认。"
            )
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 30},
        })

    async def mutate_final_request(request):
        payload = json.loads(request.content)
        if payload["messages"][0]["role"] != "system" or not isinstance(
            payload["messages"][-1]["content"], list,
        ):
            return
        if mutation == "raw_hash":
            path = sorted(image_spool.directory(core.store).glob("*.raw"))[1]
            value = path.read_bytes()
            path.write_bytes(value[:-1] + bytes([value[-1] ^ 1]))

    async with (
        source_window(core, tmp_path, source, live=True, approved=True,
                      rich_input_scope="references_v1", scope_options=opts)
        as (window, client, protocol, _, sends),
        httpx.AsyncClient(transport=httpx.MockTransport(model),
                          event_hooks={"request": [mutate_final_request]}) as model_http,
    ):
        core.model = ModelClient(core.settings, model_http)
        core.content.options = core.settings.content
        await window.capture_once(client, protocol)
        try:
            await window.approve_captured(
                client, capture_approval(window), image_resolver=resolve,
                image_transport=httpx.MockTransport(cdn),
            )
        except httpx.HTTPStatusError as error:
            raise AssertionError(error.response.text) from error
        job = core.store.one(
            "SELECT j.* FROM jobs j JOIN content_items c ON c.owner_event_id=j.event_id LIMIT 1",
        )
        yield (
            window, client, protocol, sends, calls, downloads, queries, originals,
            memory_id, outsider, job,
        )
    core.memory.index.scores_async = original_scores


@pytest.mark.parametrize("no_candidates", [False, True])
def test_originals_and_image_topic_retrieval_share_paid_job_and_verified_reply(
    core, tmp_path, no_candidates,
):
    async def scenario():
        async with native_image_memory(core, tmp_path, no_candidates=no_candidates) as value:
            assert not value.calls and len(value.downloads) == 2
            reopened = Store(core.store.path)
            try:
                rows = reopened.rows("SELECT * FROM content_items WHERE kind='qq_image' "
                                     "ORDER BY slot")
                assert [image_spool.get(reopened, row, core.clock(), 50000)[0]
                        for row in rows] == value.originals
            finally:
                reopened.close()
            assert await value.worker.run_once("native-image-memory")
            assert len(value.calls) == 2, core.store.one(
                "SELECT last_error_code FROM jobs WHERE job_id=?", (value.job["job_id"],))
            final = value.calls[-1]["messages"]
            images = [part for message in final if isinstance(message["content"], list)
                      for part in message["content"] if part["type"] == "image_url"]
            assert [base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
                    for part in images] == value.originals
            assert QUESTION in encode(final)
            returned = json.loads(next(message["content"] for message in final
                                      if message["role"] == "tool"))
            assert [row["memory_id"] for row in returned["facts"]] == (
                [] if no_candidates else [value.memory_id])
            requested = next(message["tool_calls"] for message in final
                             if message.get("tool_calls"))[0]
            assert json.loads(requested["function"]["arguments"])["query"] == CUES
            assert "过期秘密偏好" not in encode(value.calls)
            assert (PREFERENCE in encode(final)) == (not no_candidates)
            assert core.store.rows("SELECT scenario,state FROM model_usage ORDER BY rowid") == [
                {"scenario": "chat", "state": "SETTLED"}] * 2
            admissions = core.store.rows("SELECT * FROM model_admissions ORDER BY rowid")
            assert {row["logical_request_id"] for row in admissions} == {value.job["job_id"]}
            assert [row["input_bound"] for row in admissions] == [
                paid_budget.estimate_input_tokens(core.settings.paid_model, call["messages"],
                    "chat", tools=call.get("tools", ())) for call in value.calls]
            # Original bytes stay available for the final native evidence check until expiry.
            assert len(list(image_spool.directory(core.store).glob("*.raw"))) == 2
            assert (await value.entry.send(value.entry.client, value.entry.protocol))["worked"]
            assert len(value.sends) == 1
            assert len(core.store.rows("SELECT * FROM memories")) == 2
            durable = encode(core.store.rows("SELECT body_json FROM events")) + encode(
                core.store.rows("SELECT value FROM runtime_meta")) + encode(core.store.rows(
                    "SELECT conversation_json FROM agent_conversations"))
            assert all(base64.b64encode(raw).decode() not in durable for raw in value.originals)
            core.clock.advance(image_spool.RETENTION_SECONDS + 1)
            image_spool.cleanup(core.store, core.clock(), retain_completed=True)
            assert not list(image_spool.directory(core.store).glob("*.raw"))
    run(scenario())


@pytest.mark.parametrize("limit", [9, 10])
def test_five_image_memory_inputs_obey_the_explicit_model_attempt_cap(core, tmp_path, limit):
    async def scenario():
        async with native_image_memory(core, tmp_path, limit=limit) as value:
            for number in range(1, 6):
                job = value.job if number == 1 else await value.admit_image(number)
                assert await value.worker.run_once("bounded-native-image-memory")
                assert len(value.calls) == min(number * 2, limit)
                current = core.store.one("SELECT state,last_error_code FROM jobs WHERE job_id=?",
                                         (job["job_id"],))
                if number * 2 > limit:
                    assert current["state"] == "FAILED", current
                    assert current["last_error_code"] == "MODEL_DAILY_BUDGET_EXHAUSTED", current
                    assert_failure_notice(core, job["event_id"])
                else:
                    assert current["state"] == "SUCCEEDED", current
                    assert (await value.entry.send(value.entry.client,
                                                   value.entry.protocol))["worked"]
            assert len(value.sends) == (5 if limit == 10 else 4)
            assert core.store.one("SELECT COUNT(*) n FROM model_admissions")["n"] == limit
            assert core.store.one("SELECT COUNT(DISTINCT owner_event_id) n FROM content_items "
                                  "WHERE kind='qq_image'")["n"] == 5
            if limit == 10:
                event = await value.entry.admit(6, "预算已满后的新输入")
                assert await value.worker.run_once("no-eleventh-model-call")
                refused = core.store.one("SELECT state,last_error_code FROM jobs WHERE event_id=?",
                                         (event["event_id"],))
                assert refused["state"] == "FAILED", refused
                assert refused["last_error_code"] == "MODEL_DAILY_BUDGET_EXHAUSTED", refused
                assert len(value.calls) == limit
                assert core.store.one("SELECT COUNT(*) n FROM model_admissions")["n"] == limit
    run(scenario())


@pytest.mark.parametrize("mutation", [
    "raw_hash", "expired", "erased_current", "unknown_tool_request",
])
def test_joint_image_memory_flow_stops_before_final_io_on_lost_evidence(core, tmp_path, mutation):
    async def scenario():
        async with native_image_memory(core, tmp_path, mutation=mutation) as value:
            assert await value.worker.run_once("native-image-source-fence")
            assert len(value.calls) <= 1
            assert not value.sends
            if mutation in {"raw_hash", "expired"}:
                assert_failure_notice(core)
            else:
                assert not core.store.one("SELECT 1 FROM outbox")
            assert core.store.one("SELECT state FROM jobs WHERE job_id=?",
                (value.job["job_id"],))["state"] == (
                    "CANCELLED" if mutation == "erased_current" else "FAILED")
            usage = core.store.rows("SELECT * FROM model_usage")
            if mutation == "unknown_tool_request":
                assert len(usage) == 1 and usage[0]["state"] == "UNKNOWN"
                reserved = core.store.one("SELECT reserved_micro_usd FROM model_admissions")
                assert usage[0]["billed_micro_usd"] == reserved["reserved_micro_usd"] > 0
            assert not await value.worker.run_once("no-image-replay")
            core.clock.advance(image_spool.RETENTION_SECONDS + 1)
            image_spool.cleanup(core.store, core.clock(), retain_completed=True)
            assert not list(image_spool.directory(core.store).glob("*.raw"))
            assert core.store.rows("SELECT * FROM model_usage") == usage
    run(scenario())

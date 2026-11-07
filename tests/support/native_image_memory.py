"""Original image upload and native SDK memory tool; only external transports are synthetic."""

import io
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
from PIL import Image

from companion import image_spool
from companion.contracts import Candidate
from companion.image_input import ImageInputPolicy
from companion.store import after
from companion_onebot_bridge import pending_media
from tests.sdk_reader_fixture import answer, call, worker_for
from tests.support.native_consumer_entry import environment
from tests.support.onebot_images import image_segment

PREFERENCE = "我挑选耳机优先考虑轻便和隔音。"
CUES = "图片1：红色轻便头戴耳机；图片2：蓝色头戴耳机。检索主题：耳机、轻便、隔音。"
QUESTION = "结合我以前的需求，这两张图里的哪一个更合适？"


@asynccontextmanager
async def native_image_memory(core, tmp_path, *, no_candidates=False, limit=None, mutation=None,
                              core_options=None, reply=None, record_visible=False, history=False):
    originals, calls, downloads = [], [], []
    for color in ("red", "blue"):
        stream = io.BytesIO()
        Image.new("RGB", (3, 2), color).save(stream, format="PNG")
        originals.append(stream.getvalue())
    policy = ImageInputPolicy(approval_sha256="e" * 64, estimated_tokens_per_image=100,
        max_bytes_per_image=50000, max_pixels_per_image=100, max_images_per_request=2)
    paid = {"image_input": policy, "per_request_calls": 2,
            "window_calls": limit or 100, "daily_calls": limit or 100}

    async def resolver(host, port):
        return ["1.1.1.1"]

    def cdn(request):
        downloads.append(request)
        data = originals[0 if request.url.path.endswith("red.png") else 1]
        return httpx.Response(200, content=data, headers={"Content-Type": "image/png"})

    def model(request):
        payload = json.loads(request.content)
        calls.append(payload)
        returned = [message for message in payload["messages"] if message["role"] == "tool"]
        if not returned:
            body = answer(calls=[call("observe-current-image", "record_image_observation",
                {"slot": 0, "text": "图中文字：青栎资料卡，十二月九日入场，座位B17。"})]
                if record_visible else [call("recall-image-topic", "memory_recall",
                                             {"query": CUES})])
            if mutation == "unknown_tool_request":
                body.pop("usage")
        else:
            body = answer(reply or (
                "图1可见红色。按你提到的轻便需求更倾向图1；隔音仅凭图片无法确认。"))
        if "usage" in body:
            body["usage"] = {"prompt_tokens": 200, "completion_tokens": 30}
        return httpx.Response(200, json=body)

    async with environment(core, tmp_path,
        purposes=("chat", "memory", "image", "content", *(("history",) if history else ())),
        core_options={"content_enabled": True, "observation_enabled": True,
                      "lease_seconds": 600 if mutation == "expired" else 120,
                      **(core_options or {})},
        paid_options=paid) as (entry, sends):
        ids = []
        for number, content in ((900, PREFERENCE), (901, "过期秘密偏好：紫色键盘。")):
            source = await entry.admit(number, content)
            with core.store.transaction():
                ids.append(core.memory.add(Candidate(subject="user", kind="preference",
                    fact_key=str(number), content=content, evidence_event_id=source["event_id"],
                    evidence_quote=content), core.clock(), confirmed=True))
        # Native owner_private scope uses real current fact validity, not a legacy approved-ID list.
        core.store.execute("UPDATE memories SET valid_to=? WHERE memory_id=?",
                           (after(core.clock(), -1), ids[1]))
        if no_candidates:
            core.store.execute("UPDATE memories SET valid_to=? WHERE memory_id=?",
                               (after(core.clock(), -1), ids[0]))

        async def admit_image(number):
            event = await entry.admit(number, message=[
                {"type": "text", "data": {"text": QUESTION}},
                image_segment("https://gchat.qpic.cn/red.png"),
                image_segment("https://gchat.qpic.cn/blue.png"),
            ])
            row = pending_media._execute(entry.runtime,
                "SELECT * FROM automatic_media WHERE event_id=?", (event["event_id"],)).fetchone()
            pump = pending_media.MediaPump(entry.runtime, entry.client,
                image_transport=httpx.MockTransport(cdn), image_resolver=resolver)
            await pump.execute(row)
            assert not pump.pending(event["event_id"])
            return core.store.one("SELECT * FROM jobs WHERE event_id=?", (event["event_id"],))

        job = await admit_image(1)
        async with worker_for(core, model) as worker:
            async def mutate(request):
                payload = json.loads(request.content)
                if mutation == "raw_hash":
                    path = sorted(image_spool.directory(core.store).glob("*.raw"))[1]
                    data = path.read_bytes()
                    path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
                elif mutation == "expired" and any(
                    message["role"] == "tool" for message in payload["messages"]
                ):
                    # Expire already-bound originals while the worker lease remains valid.
                    core.clock.advance(image_spool.RETENTION_SECONDS + 1)
                    assert core.store.one("SELECT lease_until FROM jobs WHERE job_id=?",
                        (job["job_id"],))["lease_until"] > core.clock()
                elif mutation == "erased_current" and any(
                    message["role"] == "tool" for message in payload["messages"]
                ):
                    with core.store.transaction():
                        core.ledger.expire_events(core.store, [job["event_id"]], core.clock())

            worker.model.client.event_hooks["request"].append(mutate)
            yield SimpleNamespace(entry=entry, sends=sends, calls=calls, downloads=downloads,
                originals=originals, memory_id=ids[0], outsider=ids[1], job=job,
                admit_image=admit_image, worker=worker)

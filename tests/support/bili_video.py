import hashlib
import io
import json
from contextlib import asynccontextmanager
from datetime import timedelta, timezone

import av
import httpx
from PIL import Image

from companion.bili_video import BiliVideoService
from companion.bili_video_config import BiliVideoConfig
from companion.bili_video_protocol import (
    CAPABILITY,
    assets_to_wire,
    json_sha256,
    selection_from_wire,
    target_from_wire,
)
from companion.cloud_budget import CloudBudgetConfig
from companion.config import ObservationAdapterConfig
from companion.media_config import ContentConfig
from companion.model import ModelClient
from companion.reading_protocol import ReadObservation
from companion.video_media_contract import AssetManifest, MediaAsset
from tests.support.native_consumer import admit, select_tools, source_window
from tests.support.onebot_events import raw_event
from tests.support.reading_protocol import envelope
from tests.support.reading_service import queued
from tests.support.video_understanding import scope_options


def metadata(core, *, deadline=None, duration=60, enabled=True, grant=None):
    core.settings.bili_video = BiliVideoConfig(enabled=enabled)
    adapter, item, body = queued(core, "bilibili", grant=grant)
    core.settings.exploration_daily_bytes = 200_000_000
    core.settings.exploration_daily_requests = 100
    core.settings.exploration_monthly_requests = 1000
    core.settings.exploration_daily_deep_reads = 20
    content = core.store.one(
        'SELECT * FROM content_items WHERE read_request_id=%s', (item["request_id"],)
    )
    assert content is not None
    if deadline is not None:
        core.store.execute(
            'UPDATE content_items SET deadline=%s WHERE item_id=%s', (deadline, content["item_id"])
        )
    evidence = body["observations"][0]["evidence"]
    evidence.update(
        video={"bvid": "BV1xx411c7mD", "cid": 123, "part": 1, "duration_seconds": duration},
        duration_source="view_first_part",
        subtitles=[
            {
                "start_seconds": 0,
                "end_seconds": 2.5,
                "text": "恒星的颜色与温度有关",
                "partial": False,
            }
        ],
    )
    result = core.reading.complete(
        adapter, item["request_id"], ReadObservation.model_validate(body)
    )
    module = BiliVideoService(core.store, core.settings, core.exploration, core.model, core.clock)
    return module, content["item_id"], result["event_ids"][0]


def binding(item):
    body = {
        "schema_version": 1,
        "lease_token": item["lease_token"],
        "lease_sha256": json_sha256(item),
    }
    if item.get("source_grant") is not None:
        body["source_grant"] = item["source_grant"]
    return body


def preparation(item, *, duration=None):
    media = item["media"]
    duration = duration if duration is not None else media["metadata_duration_seconds"] + 0.04
    return {
        **binding(item),
        "identity": {key: media[key] for key in ("bvid", "cid", "part")}
        | {"duration_seconds": duration},
        "proof": {
            "resolver": "bilibili_wbi_playurl_v1",
            "view_response_sha256": "a" * 64,
            "nav_response_sha256": "b" * 64,
            "playinfo_response_sha256": "c" * 64,
            "representation_sha256": "d" * 64,
            "locator_sha256": "e" * 64,
            "mime_type": "video/mp4",
            "codec": "avc1.640028",
            "width": 640,
            "height": 360,
            "declared_bytes": None,
            "duration_milliseconds": round(duration * 1000),
        },
    }


def receipt(item, phase, body):
    return {**binding(item), "phase": phase, "request_sha256": json_sha256(body)}


def media_bytes(kind, duration=0):
    picture = Image.new("RGB", (16, 16), "blue")
    output = io.BytesIO()
    if kind == "frame":
        picture.save(output, format="PNG")
        return output.getvalue(), 1
    count = round(duration * 25)
    with av.open(output, mode="w", format="mp4") as container:
        stream = container.add_stream("libx264", rate=25)
        stream.width = stream.height = 16
        stream.pix_fmt = "yuv420p"
        for index in range(count):
            frame = av.VideoFrame.from_image(picture)
            frame.pts = index
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return output.getvalue(), count


def upload_body(item, prepared, selected=None):
    target = target_from_wire(prepared["target"])
    selection = selection_from_wire(selected or prepared["selection"])
    kind = "frame" if selection.kind == "preview" else "clip"
    ranges = (
        [(at, at) for at in selection.frame_times]
        if kind == "frame"
        else [(clip.start_seconds, clip.end_seconds) for clip in selection.clips]
    )
    assets = []
    for index, (start, end) in enumerate(ranges):
        data, count = media_bytes(kind, end - start)
        stamps = tuple(start + position / 25 for position in range(count))
        manifest = AssetManifest(
            asset_id=f"{kind}-{index}",
            kind=kind,
            target_sha256=target.sha256,
            selection_sha256=selection.sha256,
            input_sha256="f" * 64,
            requested_start_seconds=start,
            requested_end_seconds=end,
            actual_start_seconds=stamps[0],
            actual_end_seconds=stamps[-1],
            source_pts_seconds=stamps,
            output_duration_seconds=0 if kind == "frame" else count / 25,
            mime_type="image/png" if kind == "frame" else "video/mp4",
            codec="png" if kind == "frame" else "h264",
            width=16,
            height=16,
            frame_count=count,
            byte_length=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        assets.append(MediaAsset(manifest, data))
    body = {
        **binding(item),
        "prepare_sha256": prepared["prepare_sha256"],
        "stage": selection.kind,
        "selection_sha256": selection.sha256,
        "download": {
            "input_sha256": "f" * 64,
            "input_bytes": 1000,
            "mime_type": "video/mp4",
            "source_range": {"start_seconds": 0, "end_seconds": target.duration_seconds},
        },
        "assets": assets_to_wire(tuple(assets)),
    }
    return json.loads(json.dumps(body))


def final_result(
    item, prepared=None, phases=(), *, status="FAILED", used_requests=4, used_bytes=1500
):
    return {
        **binding(item),
        "status": status,
        "used_requests": used_requests if status != "UNKNOWN" else None,
        "used_bytes": used_bytes if status != "UNKNOWN" else None,
        "observed_requests": used_requests,
        "observed_bytes": used_bytes,
        "prepare_sha256": prepared["prepare_sha256"] if prepared else None,
        "phase_receipt_sha256": [phase["receipt_sha256"] for phase in phases],
        "error_code": None if status == "SUCCESS" else "SYNTHETIC_READER_FAILURE",
    }


def paid_options(core, *, cloud=None, short_window=False, call_limit=4):
    config = BiliVideoConfig(enabled=True)
    options = scope_options(core, model="synthetic-bili-video")
    options.pop("image_scope")
    options.pop("image_approval_sha256")
    options.update(
        link_scope="owner_links_v1",
        bili_video_scope=CAPABILITY,
        bili_video_policy_sha256=json_sha256(config.model_dump(mode="json")),
    )
    options["core_options"].update(
        bili_video=config,
        cloud_budget=cloud or CloudBudgetConfig(),
        content=ContentConfig(use_chat_model_for_vision=True, image_daily_micro_usd=1_000_000),
        observation_adapters={
            "bili": ObservationAdapterConfig(
                source="bilibili",
                media_analysis=True,
                reader_max_requests=8,
                token="synthetic-bili-reader-token-123456",
            )
        },
        exploration_daily_bytes=200_000_000,
        exploration_daily_requests=100,
        exploration_monthly_requests=1000,
        exploration_daily_deep_reads=20,
        exploration_max_response_bytes=1_000_000,
    )
    options["paid_options"]["image_input"] = options["paid_options"]["image_input"].model_copy(
        update={
            "max_images_per_request": 4,
            "max_pixels_per_image": 512 * 512,
            "max_bytes_per_image": 4_000_000,
        }
    )
    options["paid_options"]["per_request_calls"] = call_limit
    if short_window:
        options["window_options"]["ends_at"] = (
            (core.clock.now + timedelta(seconds=45))
            .astimezone(timezone(timedelta(hours=8)))
            .isoformat()
        )
    return options


@asynccontextmanager
async def paid_media(
    core,
    tmp_path,
    provider,
    *,
    controls=None,
    followup_before_metadata=False,
    two_links=False,
    **options,
):
    async def source(ws):
        text = "请看看这个视频 https://www.bilibili.com/video/BV1xx411c7mD"
        if two_links:
            text += " https://www.bilibili.com/video/BV1yy411c7mE"
        await ws.send(json.dumps(raw_event(time=int(core.clock.now.timestamp()), text=text)))
        await ws.wait_closed()

    async with (
        source_window(core, tmp_path, source,
            scope_options=paid_options(core, **{"call_limit": 5,
                **{k: v for k, v in options.items() if k != "cloud"}}),
        ) as (window, client, protocol, _, sends),
        httpx.AsyncClient(transport=httpx.MockTransport(provider)) as provider_http,
    ):
        core.content.options = core.settings.content
        core.model = ModelClient(core.settings, provider_http)
        core.bili_video.model = core.model
        await window.capture_once(client, protocol)
        assert await window.runtime.flush_one(client)
        urls = [row["target_url"] for row in core.store.rows(
            "SELECT target_url FROM content_items")]
        await select_tools(core, "read_content", urls)
        for index in range(2 if two_links else 1):
            item = core.reading.lease("bili")
            assert item is not None and "media" not in item
            body = envelope("bilibili")
            observed = body["observations"][0]
            body["lease_token"] = item["lease_token"]
            observed["provenance"] = item["context"]
            observed["url"] = observed["original_url"] = item["url"]
            observed["fetched_at"] = observed["evidence"]["fetched_at"] = core.clock()
            observed["evidence"].update(
                video={
                    "bvid": item["url"].rpartition("/")[2],
                    "cid": 123 + index,
                    "part": 1,
                    "duration_seconds": 60,
                },
                duration_source="view_first_part",
                subtitles=[
                    {
                        "start_seconds": 0,
                        "end_seconds": 2.5,
                        "text": "恒星的颜色与温度有关",
                        "partial": False,
                    }
                ],
            )
            if followup_before_metadata and index == 0:
                await admit(client, core, window, 2, "继续读一下")
            core.reading.complete("bili", item["request_id"], ReadObservation.model_validate(body))
        await select_tools(core, "analyze_shared_video", urls)
        if options.get("cloud") is not None:
            from tests.support.cloud_budget import change

            change(core, **options["cloud"].model_dump())
        child = core.reading.lease("bili", capabilities=[CAPABILITY])
        assert child is not None and "media" in child
        module = core.bili_video
        prepare = module.prepare("bili", child["request_id"], preparation(child))
        if controls is not None:
            controls.update(window=window, client=client, url=urls[0])
        selectors = core.store.rows("SELECT * FROM model_usage WHERE scenario='chat'")
        assert len(selectors) == 2 and all(row["state"] == "SETTLED" for row in selectors)
        yield module, child, prepare
        for row in selectors:
            assert core.store.one('SELECT * FROM model_usage WHERE request_id=%s',
                                  (row["request_id"],)) == row
        assert sends == []


def model_response(content, *, tokens=100):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": tokens, "completion_tokens": 20},
        },
    )


def phase_usage(core):
    return core.store.rows("SELECT * FROM model_usage WHERE scenario LIKE %s "
                           "ORDER BY record_sequence", ("content_video%",))


def phase_costs(core):
    return core.store.rows("SELECT * FROM cloud_cost_snapshots WHERE task_kind='video'")

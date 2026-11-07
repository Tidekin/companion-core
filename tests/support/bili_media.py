import ssl
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import httpcore

from companion.bili_video_protocol import DEFAULT_LIMITS
from source_reader.network import PublicNetwork
from tests.support.source_reader import Responses

NOW = datetime(2026, 10, 24, tzinfo=UTC)


BVID = "BV1234567890"


CDN = "https://cn-test.bilivideo.com/upgcxcode/source.m4s?deadline=123&token=transient"


VIEW = {"code": 0, "data": {
    "bvid": BVID, "cid": 123, "title": "Example", "desc": "A source description",
    "pages": [{"page": 1, "cid": 123, "duration": 3}],
}}


NAV = {"code": 0, "data": {"wbi_img": {
    "img_url": "https://i0.hdslb.com/bfs/wbi/" + "1" * 32 + ".png",
    "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "2" * 32 + ".png",
}}}


PLAY = {"code": 0, "data": {"timelength": 3000, "dash": {"video": [{
    "baseUrl": CDN, "mimeType": "video/mp4", "codecs": "avc1.64001e",
    "width": 64, "height": 48, "bandwidth": 1000,
}]}}}


def lease(**changes):
    until = (NOW + timedelta(seconds=180)).isoformat()
    return {
        "schema_version": 1, "request_id": "media_fixture", "lease_token": "lease_media_fixture",
        "lease_until": until, "operation": "read",
        "url": "https://www.bilibili.com/video/" + BVID,
        "max_bytes": DEFAULT_LIMITS.max_input_bytes + 1048576,
        "max_requests": 8, "max_items": 1, "allowed_hosts": [],
        "context": {"root_task_id": "root_media", "source": "bilibili",
                    "source_event_ids": ["observation_source"], "privacy_epoch": 1},
        "media": {
            "capability": "bounded_video_media_v1", "content_item_id": "content_" + "1" * 32,
            "source_event_id": "observation_source", "source_sha256": "a" * 64,
            "bvid": BVID, "cid": 123, "part": 1, "metadata_duration_seconds": 3,
            "metadata_duration_source": "view_first_part", "deadline": until,
            "limits": asdict(DEFAULT_LIMITS),
        }, **changes,
    }


def pool(*responses):
    backend = Responses(*responses)

    async def addresses(host, port):
        return ["93.184.216.34"]

    value = httpcore.AsyncConnectionPool(
        ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        network_backend=PublicNetwork(resolver=addresses, backend=backend),
        max_keepalive_connections=0, retries=0,
    )
    return value, backend


async def allowed():
    return None

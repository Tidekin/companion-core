import json
import ssl

import httpcore

from source_reader.bilibili import BilibiliReader
from source_reader.network import PublicHTTP
from source_reader.worker import read_result
from tests.support.exploration import URL

NOW = "2026-10-19T04:00:00+00:00"


CAPTION = "https://aisubtitle.hdslb.com/bfs/subtitle/example.json?auth_key=transient-reference"


ITEM = {
    "lease_token": "lease_fixture",
    "operation": "read",
    "url": URL,
    "max_bytes": 50000,
    "max_requests": 4,
    "max_items": 1,
}


META = {
    "code": 0,
    "data": {
        "title": "星空的颜色",
        "desc": "公开简介：讨论恒星与光谱。",
        "cid": 1234,
        "owner": {"name": "公开作者"},
    },
}


PLAYER = {
    "code": 0,
    "data": {"subtitle": {"subtitles": [{"lan": "zh-CN", "subtitle_url": CAPTION}]}},
}


SUBTITLE = {
    "body": [
        {"from": 0, "to": 2.5, "content": "恒星的颜色与温度有关。"},
        {"from": 4, "to": 6, "content": "这里有两秒没有字幕。"},
    ]
}


def response(body=b"", status=200, headers=None):
    if isinstance(body, dict):
        body = json.dumps(body, ensure_ascii=False).encode()
    headers = {"Content-Length": str(len(body)), "Connection": "close", **(headers or {})}
    return (
        f"HTTP/1.1 {status} test\r\n"
        + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        + "\r\n"
    ).encode() + body


class RecordingStream(httpcore.AsyncMockStream):
    def __init__(self, raw, backend):
        super().__init__([raw])
        self.backend = backend

    async def write(self, buffer, timeout=None):  # noqa: ASYNC109
        if buffer:
            self.backend.requests.append(buffer)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):  # noqa: ASYNC109
        self.backend.server_names.append(server_hostname)
        assert ssl_context.check_hostname
        return self


class Responses(httpcore.AsyncNetworkBackend):
    def __init__(self, *rows):
        self.rows, self.requests, self.server_names, self.hosts = list(rows), [], [], []

    async def connect_tcp(self, host, port, **kwargs):
        self.hosts.append(host)
        row = self.rows.pop(0)
        if isinstance(row, Exception):
            raise row
        return RecordingStream(row, self)


async def read(*rows, item=None):
    backend = Responses(*rows)
    pool = httpcore.AsyncConnectionPool(
        ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        network_backend=backend,
        max_keepalive_connections=0,
    )
    async with PublicHTTP(pool) as public:
        result = await read_result(BilibiliReader(public, clock=lambda: NOW), item or ITEM)
    return result, backend

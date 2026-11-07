import json
from datetime import UTC, datetime, timedelta
from html import escape
from urllib.parse import parse_qs, urlsplit

import httpcore

from source_reader.accounts import QZONE_COMMIT
from source_reader.qzone import ENDPOINT

NOW = datetime(2026, 10, 31, 0, 0, tzinfo=UTC)


URL = "https://user.qzone.qq.com/12345678"


TOKEN = "qzone-reader-synthetic-" * 3


def account_file(path, now=NOW):
    grant = {"grant_id": "grant_" + "e" * 32, "epoch": 1, "fingerprint": "a" * 64}
    expires = (now + timedelta(hours=1)).isoformat(timespec="microseconds")
    path.write_text(json.dumps({"enabled": True, "expires_at": expires,
        "upstream_commit": QZONE_COMMIT, "cookies": {"uin": "o87654321",
        "p_skey": "synthetic-private-key"}, "authorization": grant}), encoding="utf-8")
    path.chmod(0o600)
    return {**grant, "expires_at": expires}


def response(now=NOW, **changes):
    post = {"uin": 12345678, "tid": "synthetic-tid-one", "content": "今天看到的星空很美。",
            "created_time": int(now.timestamp()) - 60, **changes}
    def attr(key):
        return escape(str(post[key]), quote=True)
    body = (f'<li class="f-single" id="feed_{attr("uin")}_311_0_1780000000_0_1">'
            f'<div class="f-info">{escape(post["content"])}</div>'
            f'<i name="feed_data" data-uin="{attr("uin")}" data-tid="{attr("tid")}" '
            f'data-abstime="{attr("created_time")}"></i>'
            '<div class="txt-box">UNREQUESTED_UI</div>'
            '<div class="f-ct"><div class="f-info">UNREQUESTED_COMMENT</div></div></li>')
    return {"code": 0, "data": {"data": [{"html": body}]}}


def pool_for(body, calls):
    raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
    wire = (b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(raw)).encode()
            + b"\r\nConnection: close\r\n\r\n" + raw)

    class Pool(httpcore.AsyncConnectionPool):
        def stream(self, method, url, **kwargs):
            assert method == "GET" and url.startswith(ENDPOINT + "?")
            query = parse_qs(urlsplit(url).query)
            assert query["count"] == ["1"] and query["start"] == ["0"]
            assert query["uin"] == query["hostuin"] == ["12345678"]
            assert query["scope"] == ["0"] and query["format"] == ["json"]
            assert kwargs["headers"]["Referer"] == "https://user.qzone.qq.com/12345678"
            calls.append(method)
            return super().stream(method, url, **kwargs)

    return Pool(network_backend=httpcore.AsyncMockBackend([wire]))

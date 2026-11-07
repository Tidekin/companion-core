import ssl

import httpcore

from tests.support.source_reader import Responses

URL = "https://www.xiaoheihe.cn/app/bbs/link/168860244"


IMAGE = "https://imgheybox.max-c.com/bbs/example.jpg"


SOURCE_TOKEN = "separate-read-only-service-token"


ITEM = {
    "operation": "read",
    "url": URL,
    "max_bytes": 524288,
    "max_requests": 1,
    "max_items": 4,
    "lease_token": "fixture_lease",
}


POST = {
    "status": "ok",
    "result": {
        "link": {
            "linkid": 168860244,
            "title": "测试帖子",
            "text": [
                {"type": "text", "text": "<p>第一段介绍了手绘地图。</p>"},
                {"type": "img", "url": IMAGE + "?imageMogr2/resize/500"},
                {"type": "html", "content": "<script>secret()</script><p>第二段介绍独立游戏。</p>"},
            ],
        }
    },
}


def mock_pool(*rows):
    backend = Responses(*rows)
    return httpcore.AsyncConnectionPool(
        ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        network_backend=backend,
        max_keepalive_connections=0,
    ), backend

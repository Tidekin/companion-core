import hashlib

from companion.exploration import ReadResult
from source_reader.heybox import render_article
from tests.support.content import CDN, POST, owner_share


def article(core, count=8):
    owner = owner_share(core, text=POST)
    text, images, truncated = render_article(
        [
            ("text", "这是文章的真实正文，图片依次解释其中的论点。"),
            *[("image", CDN + str(i)) for i in range(count)],
        ]
    )
    lease = core.exploration.lease("reader")
    result = ReadResult.model_validate(
        {
            "lease_token": lease["lease_token"],
            "status": "SUCCESS",
            "used_requests": 1,
            "used_bytes": 1024,
            "items": [
                {
                    "title": "多图测试",
                    "url": POST,
                    "text": text,
                    "image_urls": [image["url"] for image in images],
                    "article": {
                        "method": "source_api",
                        "fetched_at": core.clock(),
                        "images": images,
                        "response_sha256": hashlib.sha256(b"fixture article").hexdigest(),
                        "response_bytes": 1024,
                        "truncated": truncated,
                    },
                }
            ],
        }
    )
    receipt = core.exploration.complete("reader", lease["request_id"], result)
    assert core.exploration.complete("reader", lease["request_id"], result)["duplicate"]
    return owner, receipt["event_ids"][0]

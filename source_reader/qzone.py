"""One authorized target's latest text post, using qzoneActive's pinned GET shape.

No AstrBot, Core storage, automatic interactions, profile actions or write transport.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from urllib.parse import urlencode

from source_reader.accounts import account_grant, source_grant
from source_reader.network import PublicHTTP, ReadFailure, response_body

ENDPOINT = ("https://user.qzone.qq.com/proxy/domain/taotao.qq.com/"
            "cgi-bin/emotion_cgi_msglist_v6")


def target_uin(url):
    if not isinstance(url, str) or not re.fullmatch(
        r"https://user\.qzone\.qq\.com/[1-9][0-9]{4,19}", url,
    ):
        raise ReadFailure("CONTENT_URL_NOT_ALLOWED")
    return url.rsplit("/", 1)[1]


class QZoneHTTP(PublicHTTP):
    def __init__(self, account_file, pool=None, clock=lambda: datetime.now(UTC)):
        super().__init__(pool)
        self.account_file, self.clock = account_file, clock
        self.blocked_grant = None

    def grant_snapshot(self, required=False):
        return source_grant(self.account_file, self.clock(), "qzone", required)

    def verify(self, budget):
        grant, fingerprint = account_grant(self.account_file, self.clock(), "qzone")
        if self.blocked_grant == fingerprint:
            raise ReadFailure("SOURCE_DISABLED")
        if budget.authorization is not None and budget.authorization != fingerprint:
            raise ReadFailure("SOURCE_CREDENTIAL_REVOKED")
        budget.authorization = fingerprint
        return grant

    async def latest(self, url, budget):
        uin = target_uin(url)
        if not callable(budget.validate_account_read):
            raise ReadFailure("SOURCE_GRANT_REQUIRED")
        await budget.validate_account_read()
        grant = self.verify(budget)
        key = grant["cookies"].get("p_skey") or grant["cookies"]["skey"]
        gtk = 5381
        for char in key:
            gtk += (gtk << 5) + ord(char)
        token = str(gtk & 0x7fffffff)
        referrer = "https://user.qzone.qq.com/" + grant["cookies"]["uin"].removeprefix("o")
        query = urlencode({"uin": uin, "ftype": 0, "sort": 0, "pos": 0, "num": 1,
                           "g_tk": token, "g_tk_2": token, "format": "json",
                           "qzreferrer": referrer})
        budget.reserve()
        try:
            async with self.pool.stream(
                "GET", ENDPOINT + "?" + query,
                headers={"Cookie": "; ".join(f"{k}={v}" for k, v in grant["cookies"].items()),
                         "Referer": referrer, "Accept-Encoding": "identity"},
                extensions=budget.request_extensions(lambda: self.verify(budget)),
            ) as response:
                raw = await response_body(response, budget)
        except ReadFailure as error:
            if str(error) == "UPSTREAM_ACCESS_LIMIT":
                self.blocked_grant = budget.authorization
            raise
        self.verify(budget)
        await budget.validate_account_read()
        self.verify(budget)
        # Account tokens and raw upstream content are never placed in evidence/logs.
        return raw


class QZoneReader:
    parser_version = "qzone-latest-text-v1"

    def __init__(self, public, clock=lambda: datetime.now(UTC)):
        self.public, self.clock = public, clock

    def grant_snapshot(self, required=False):
        return self.public.grant_snapshot(required=required)

    async def read(self, item, budget):
        if (item.get("operation") != "read" or not item.get("source_grant")
            or item.get("max_items") != 1):
            raise ReadFailure("QZONE_TARGET_SCOPE_REQUIRED")
        uin = target_uin(item["url"])
        raw = await self.public.latest(item["url"], budget)
        # The fixed platform endpoint can wrap JSON even with format=json. Accept
        # one known complete envelope as data; never execute callback content.
        envelope = re.fullmatch(rb"\s*(?:_Callback|callback)\s*\((.*)\)\s*;?\s*", raw, re.S)
        data = json.loads(envelope[1] if envelope else raw)
        if not isinstance(data, dict) or type(data.get("code")) is not int:
            raise ReadFailure("UPSTREAM_RESPONSE_INVALID")
        if data["code"] != 0:
            self.public.blocked_grant = budget.authorization
            raise ReadFailure("UPSTREAM_ACCESS_LIMIT")
        rows = data.get("msglist")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ReadFailure("READ_BODY_UNAVAILABLE")
        post = rows[0]
        author, tid, text, created = (post.get(k) for k in (
            "uin", "tid", "content", "created_time"))
        if (type(author) not in {str, int} or str(author) != uin
            or not isinstance(tid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tid)
            or not isinstance(text, str) or not text.strip()
            or type(created) is not int or created <= 0):
            raise ReadFailure("UPSTREAM_RESPONSE_INVALID")
        now = self.clock()
        published = datetime.fromtimestamp(created, UTC)
        if published > now:
            raise ReadFailure("UPSTREAM_RESPONSE_INVALID")
        original = text.strip()
        return [{"url": item["url"], "title": "空间说说", "text": original[:12000],
                 "qzone": {"method": "account_api", "scope": "latest_post_text",
                           "author_uin": uin, "tid": tid, "published_at": published.isoformat(),
                           "fetched_at": now.isoformat(), "response_sha256": hashlib.sha256(
                               raw).hexdigest(), "response_bytes": len(raw),
                           "truncated": len(original) > 12000,
                           "images_analyzed": False, "comments_read": False}}]

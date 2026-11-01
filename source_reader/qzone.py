"""One authorized owner's latest text post, using the pinned owner timeline GET.

No AstrBot, Core storage, automatic interactions, profile actions or write transport.
"""

import hashlib
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urlencode

from source_reader.accounts import account_grant, source_grant
from source_reader.network import PublicHTTP, ReadFailure, response_body

ENDPOINT = ("https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/"
            "cgi-bin/feeds/feeds_html_act_all")


class _Timeline(HTMLParser):
    """Read feed metadata and owner body nodes, excluding comments and UI nodes."""

    void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr"}
    excluded = {"f-ct", "f-single-foot", "comments", "comments-list"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.rows, self.row = [], [], None

    def handle_starttag(self, tag, attrs):
        values = {}
        for key, value in attrs:
            if key in values and (key in {"id", "name"} or key.startswith("data-")):
                raise ReadFailure("UPSTREAM_RESPONSE_INVALID")
            # HTML's duplicate-attribute rule keeps the first value. QQ's UI
            # markup repeats class attributes; identity metadata stays strict.
            values.setdefault(key, value)
        classes = set((values.get("class") or "").split())
        blocked = (tag in {"script", "style", "iframe"} or bool(classes & self.excluded)
                   or bool(self.stack and self.stack[-1][1]))
        depth = len(self.stack) + 1
        if tag not in self.void:
            self.stack.append((tag, blocked))
        identity = values.get("id") or ""
        if identity.startswith("feed_"):
            match = re.fullmatch(r"feed_(\d+)_(\d+)_(\d+)_(\d+)_\d+_\d+", identity)
            if self.row is not None or not match or blocked:
                raise ReadFailure("UPSTREAM_RESPONSE_INVALID")
            self.row = {"author": match[1], "appid": match[2], "typeid": match[3],
                        "depth": depth, "markers": [], "bodies": [], "closed": False}
            self.rows.append(self.row)
        if self.row is None or blocked:
            return
        if values.get("name") == "feed_data":
            self.row["markers"].append(values)
        if "f-info" in classes:
            self.row["bodies"].append({"depth": depth, "parts": [], "closed": False})
        if tag == "br":
            self.handle_data("\n")

    def handle_data(self, data):
        if self.row is not None and not (self.stack and self.stack[-1][1]):
            for body in self.row["bodies"]:
                if not body["closed"]:
                    body["parts"].append(data)

    def handle_endtag(self, tag):
        tags = [value[0] for value in self.stack]
        if tag not in tags:
            return
        index = len(tags) - 1 - tags[::-1].index(tag)
        if self.row is not None:
            for body in self.row["bodies"]:
                if body["depth"] > index:
                    body["closed"] = True
            if self.row["depth"] > index:
                self.row["closed"] = True
                self.row = None
        self.stack = self.stack[:index]


def latest_post(raw, uin, now):
    # The platform returns a JS data object with `undefined` in unrelated fields.
    # Reuse the primary implementation's static HTML unescaping, never JS execution.
    envelope = re.fullmatch(rb"\s*(?:_Callback|callback)\s*\((.*)\)\s*;?\s*", raw, re.S)
    text = (envelope[1] if envelope else raw).decode("utf-8-sig").strip()
    if not (text.startswith("{") and text.endswith("}")):
        raise ReadFailure("UPSTREAM_RESPONSE_INVALID")
    status = re.search(r'''(?:\bcode|["']code["'])\s*:\s*(-?\d+)\s*(?=[,}])''', text[:1024])
    if not status:
        raise ReadFailure("UPSTREAM_RESPONSE_INVALID")
    if int(status[1]) != 0:
        raise ReadFailure("UPSTREAM_ACCESS_LIMIT")
    for escaped, literal in ((r'\x22', '"'), (r'\x3C', '<'), (r'\x3c', '<'),
                             (r'\x27', "'"), (r'\"', '"'), (r"\'", "'"),
                             (r'\/', '/'), (r'\n', '\n'), (r'\r', '\r'), (r'\t', '\t')):
        text = text.replace(escaped, literal)
    parser = _Timeline()
    parser.feed(text)
    posts = []
    for row in parser.rows:
        if row["author"] != uin or not row["closed"] or len(row["markers"]) != 1:
            raise ReadFailure("UPSTREAM_RESPONSE_INVALID")
        marker = row["markers"][0]
        if marker.get("data-uin") != uin:
            raise ReadFailure("UPSTREAM_RESPONSE_INVALID")
        if row["appid"] != "311":
            continue
        tid, created = marker.get("data-tid", ""), marker.get("data-abstime", "")
        if tid.isdecimal():
            tid = marker.get("data-fkey") or tid
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tid)
            or not re.fullmatch(r"[1-9][0-9]{0,11}", created)):
            raise ReadFailure("UPSTREAM_RESPONSE_INVALID")
        published = datetime.fromtimestamp(int(created), UTC)
        if published > now:
            raise ReadFailure("UPSTREAM_RESPONSE_INVALID")
        posts.append((published, tid, row))
    if not posts:
        raise ReadFailure("READ_BODY_UNAVAILABLE")
    published, tid, row = max(posts, key=lambda post: post[0])
    if len([post for post in posts if post[0] == published]) != 1:
        raise ReadFailure("UPSTREAM_RESPONSE_INVALID")
    marker = row["markers"][0]
    if (row["typeid"] == "5" or marker.get("data-origtid") not in (None, "", tid)
        or marker.get("data-origuin") not in (None, "", "0", uin)):
        raise ReadFailure("READ_BODY_UNAVAILABLE")
    bodies = {"".join(body["parts"]).strip() for body in row["bodies"] if body["closed"]}
    bodies.discard("")
    if len(bodies) != 1:
        raise ReadFailure("READ_BODY_UNAVAILABLE")
    return tid, published, bodies.pop()


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
        referrer = "https://user.qzone.qq.com/" + uin
        query = urlencode({"uin": uin, "hostuin": uin, "scope": 0, "filter": "all",
                           "flag": 1, "refresh": 0, "firstGetGroup": 0, "mixnocache": 0,
                           "scene": 0, "refer": 2, "start": 0, "count": 1,
                           "sidomain": "qzonestyle.gtimg.cn", "useutf8": 1,
                           "outputhtmlfeed": 1, "g_tk": token, "format": "json"})
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
    parser_version = "qzone-owner-timeline-text-v2"

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
        now = self.clock()
        try:
            tid, published, original = latest_post(raw, uin, now)
        except ReadFailure as error:
            if str(error) == "UPSTREAM_ACCESS_LIMIT":
                self.public.blocked_grant = budget.authorization
            raise
        return [{"url": item["url"], "title": "空间说说", "text": original[:12000],
                 "qzone": {"method": "account_api", "scope": "latest_post_text",
                           "author_uin": uin, "tid": tid, "published_at": published.isoformat(),
                           "fetched_at": now.isoformat(), "response_sha256": hashlib.sha256(
                               raw).hexdigest(), "response_bytes": len(raw),
                           "truncated": len(original) > 12000,
                           "images_analyzed": False, "comments_read": False}}]

"""Explicitly scoped public GETs followed by offline HTML extraction."""

import hashlib
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import urljoin, urlsplit, urlunsplit

from companion.source_policy import web_host_allowed
from source_reader.network import PublicHTTP, ReadFailure, response_body

TRAFILATURA_VERSION = "2.3.1"


def web_url(value, allowed_hosts):
    if (not isinstance(value, str) or len(value) > 2048 or "\\" in value
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        raise ReadFailure("CONTENT_URL_NOT_ALLOWED")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https" and web_host_allowed(parsed.hostname, allowed_hosts)
            and parsed.username is None and parsed.password is None
            and parsed.port in {None, 443} and not parsed.fragment
            and re.fullmatch(r"[a-z0-9.-]+", parsed.hostname or "")
        )
    except ValueError:
        valid = False
    if not valid:
        raise ReadFailure("CONTENT_URL_NOT_ALLOWED")
    return urlunsplit(("https", parsed.hostname, parsed.path or "/", parsed.query, ""))


class WebHTTP(PublicHTTP):
    """Reuse DNS pinning and byte/request budgets, with a separate exact host scope."""

    def __init__(self, allowed_hosts, pool=None):
        super().__init__(pool)
        self.allowed_hosts = frozenset(allowed_hosts)
        if not self.allowed_hosts:
            raise ValueError("An explicit host scope is required")

    async def get(self, url, budget, role="page"):
        url = web_url(url, self.allowed_hosts)
        for hop in range(4):
            budget.reserve()
            async with self.pool.stream(
                "GET", url,
                headers={"Accept-Encoding": "identity", "Accept": "text/html",
                         "User-Agent": "CompanionSourceReader/0.6"},
                extensions=budget.request_extensions(),
            ) as response:
                header_bytes = sum(len(k) + len(v) + 4 for k, v in response.headers)
                budget.used_bytes += header_bytes
                if header_bytes > 8192 or budget.used_bytes > budget.max_bytes:
                    raise ReadFailure("READER_BYTE_LIMIT")
                if response.status in {301, 302, 303, 307, 308}:
                    locations = [v for k, v in response.headers if k.lower() == b"location"]
                    if hop == 3 or len(locations) != 1:
                        raise ReadFailure("READER_REDIRECT_NOT_ALLOWED")
                    location = locations[0].decode("ascii")
                    if (len(location) > 2048 or "\\" in location
                        or any(ord(char) <= 32 or ord(char) == 127 for char in location)):
                        raise ReadFailure("READER_REDIRECT_NOT_ALLOWED")
                    url = web_url(urljoin(url, location), self.allowed_hosts)
                    continue
                if response.status in {404, 410}:
                    raise ReadFailure("READ_TARGET_UNAVAILABLE")
                if response.status == 200:
                    types = [v for k, v in response.headers if k.lower() == b"content-type"]
                    if (len(types) != 1 or types[0].split(b";", 1)[0].strip().lower()
                        not in {b"text/html", b"application/xhtml+xml"}):
                        raise ReadFailure("READER_CONTENT_TYPE_UNSUPPORTED")
                body = await response_body(response, budget)
            parsed = urlsplit(url)
            budget.sources.append({
                "role": role,
                "url": urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
                "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
            })
            return body, url
        raise ReadFailure("READER_REDIRECT_NOT_ALLOWED")


class PageMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts, self.in_title, self.restricted = [], False, False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "input" and attrs.get("type", "").lower() == "password":
            self.restricted = True
        if (tag == "form" and any(token in str(attrs).lower()
                                 for token in ("captcha", "challenge"))):
            self.restricted = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, value):
        if self.in_title:
            self.title_parts.append(value)


def extract_html(document):
    """No fetch_url/crawler/metadata date lookup: the parser receives bytes only."""
    try:
        installed = version("trafilatura")
        if installed != TRAFILATURA_VERSION:
            raise ReadFailure("READER_PARSER_VERSION_UNSUPPORTED")
        from trafilatura import extract
    except (ImportError, PackageNotFoundError) as error:
        raise ReadFailure("READER_PARSER_UNAVAILABLE") from error
    return extract(
        document, output_format="txt", favor_precision=True, include_comments=False,
        include_tables=True, include_images=False, include_links=False, with_metadata=False,
    ) or ""


class WebReader:
    parser_version = "web/1;trafilatura/" + TRAFILATURA_VERSION

    def __init__(self, public, renderer=None, clock=lambda: datetime.now(UTC).isoformat()):
        self.public, self.renderer, self.clock = public, renderer, clock

    async def read(self, item, budget):
        if item["operation"] != "read":
            raise ReadFailure("READER_OPERATION_INVALID")
        raw, url = await self.public.get(item["url"], budget)
        page = PageMetadata()
        page.feed(raw.decode("utf-8", errors="replace"))
        if page.restricted:
            raise ReadFailure("UPSTREAM_ACCESS_LIMIT")
        text, method = extract_html(raw), "static_html"
        if not text.strip() and self.renderer:
            rendered = await self.renderer.html(url, raw)
            page = PageMetadata()
            page.feed(rendered.decode("utf-8", errors="replace"))
            if page.restricted:
                raise ReadFailure("UPSTREAM_ACCESS_LIMIT")
            text, method = extract_html(rendered), "offline_browser"
        if not text.strip():
            raise ReadFailure("READ_BODY_UNAVAILABLE")
        text = text.strip()
        return [{
            "url": url, "title": (" ".join("".join(page.title_parts).split()) or url)[:300],
            "text": text[:11500],
            "web": {
                "method": method, "fetched_at": self.clock(),
                "response_sha256": hashlib.sha256(raw).hexdigest(),
                "response_bytes": len(raw), "truncated": len(text) > 11500,
                "scope": "extracted_page_text", "images_analyzed": False,
            },
        }]

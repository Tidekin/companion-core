"""Bounded public HTTPS acquisition with attempt-local evidence identity."""

import asyncio
import hashlib
import ipaddress
import socket
from datetime import UTC, datetime
from itertools import zip_longest
from urllib.parse import urljoin, urlsplit

import httpx

from companion_meme_sources import FetchedResource, FetchEvidence, FetchHop


def global_address(value):
    try:
        ip = ipaddress.ip_address(value)
        return (
            ip.is_global
            and not ip.is_multicast
            and (ip.version == 4 or not (ip.ipv4_mapped or ip.sixtofour or ip.teredo))
        )
    except ValueError:
        return False


def interleave(values):
    """RFC 8305 section 4: keep the resolver's RFC 6724 order, alternating families."""
    first = [value for value in values if (":" in value) == (":" in values[0])]
    other = [value for value in values if (":" in value) != (":" in values[0])]
    return [value for pair in zip_longest(first, other) for value in pair if value is not None]


async def resolve_public(host, port):
    addresses = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
        5,
    )
    # The system order already demotes destinations this host cannot route to.
    values = list(dict.fromkeys(row[4][0] for row in addresses))
    if not values or any(not global_address(value) for value in values):
        raise ValueError("IMAGE_NONPUBLIC_ADDRESS")
    return interleave(values)


def public_url(value):
    if (not isinstance(value, str) or len(value) > 2048 or "\\" in value
        or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        raise ValueError("MEME_URL_INVALID")
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username is not None
        or url.password is not None or url.fragment or url.port not in {None, 443}):
        raise ValueError("MEME_URL_INVALID")
    try:
        ipaddress.ip_address(url.hostname)
    except ValueError:
        pass
    else:
        if not global_address(url.hostname):
            raise ValueError("MEME_NONPUBLIC_ADDRESS")
    return value


class PublicFetcher:
    """Numeric DNS pinning and bounded redirects; no cookies or ambient proxy."""

    def __init__(self, limits, usage, check, *, transport=None, resolver=resolve_public):
        self.limits, self.usage, self.check = limits, usage, check
        self.transport, self.resolver = transport, resolver
        self._issued = []

    def verify(self, resource):
        # Only this attempt's trusted fetch layer may issue acquisition evidence. A
        # provider cannot substitute an equal-shaped object built from upstream metadata.
        if not any(resource is issued for issued in self._issued):
            raise ValueError("MEME_FETCH_EVIDENCE_INVALID")
        return resource

    async def begin_query(self):
        await self.check()
        if self.usage.queries >= self.limits.queries:
            raise ValueError("MEME_QUERY_LIMIT")
        self.usage.queries += 1

    async def get(self, url, max_bytes, *, token=None, redirects=True):
        url = public_url(url)
        requested_url, hops = url, []
        if token is not None and urlsplit(url).hostname != "api.search.brave.com":
            raise ValueError("MEME_CREDENTIAL_DESTINATION")
        delegate = self.transport or httpx.AsyncHTTPTransport(trust_env=False, retries=0)
        try:
            async with asyncio.timeout(20):
                for hop in range(4):
                    await self.check()
                    target = httpx.URL(public_url(url))
                    addresses = await self.resolver(target.host, 443)
                    if not addresses or any(not global_address(ip) for ip in addresses):
                        raise ValueError("MEME_NONPUBLIC_ADDRESS")
                    await self.check()
                    if self.usage.requests >= self.limits.requests:
                        raise ValueError("MEME_REQUEST_LIMIT")
                    self.usage.requests += 1
                    headers = {"Host": target.host, "Accept-Encoding": "identity"}
                    if target.host == "api.github.com":
                        headers["User-Agent"] = "Companion-Core-meme-discovery/1"
                    if token:
                        headers["X-Subscription-Token"] = token
                    request = httpx.Request(
                        "GET", target.copy_with(host=addresses[0]), headers=headers,
                        extensions={"sni_hostname": target.host,
                                    "timeout": dict.fromkeys(
                                        ("connect", "read", "write", "pool"), 5)},
                    )

                    async def trace(name, info, request=request, target=target, pin=addresses[0]):
                        if name.endswith("send_request_headers.started"):
                            await self.check()
                            if (request.url != target.copy_with(host=pin)
                                or request.headers.get("Host") != target.host
                                or request.extensions.get("sni_hostname") != target.host):
                                raise ValueError("MEME_DESTINATION_CHANGED")

                    request.extensions["trace"] = trace
                    response = await delegate.handle_async_request(request)
                    try:
                        await self.check()
                        if sum(len(k) + len(v) for k, v in response.headers.raw) > 16384:
                            raise ValueError("MEME_HEADERS_LIMIT")
                        hops.append(FetchHop(str(target), addresses[0], response.status_code))
                        if response.status_code in {301, 302, 303, 307, 308}:
                            locations = response.headers.get_list("location")
                            if token or not redirects or hop == 3 or len(locations) != 1:
                                raise ValueError("MEME_REDIRECT_REJECTED")
                            url = public_url(urljoin(url, locations[0]))
                            continue
                        if response.status_code != 200:
                            raise ValueError("MEME_HTTP_REJECTED")
                        if response.headers.get("content-encoding", "identity").strip().lower() != (
                            "identity"
                        ):
                            raise ValueError("MEME_ENCODING_REJECTED")
                        limit = min(max_bytes,
                                    self.limits.download_bytes - self.usage.download_bytes)
                        size = int(response.headers.get("content-length", "0"))
                        if limit <= 0 or not 0 <= size <= limit:
                            raise ValueError("MEME_BYTE_LIMIT")
                        body = bytearray()
                        checked_bytes = 0
                        async for chunk in response.aiter_bytes():
                            self.usage.download_bytes += len(chunk)
                            if len(body) + len(chunk) > limit:
                                raise ValueError("MEME_BYTE_LIMIT")
                            body.extend(chunk)
                            # TCP/TLS fragmentation must not turn every small packet
                            # into a private Core round trip. Count and bound each
                            # observed fragment, then fence each bounded read block.
                            if len(body) - checked_bytes >= 8192:
                                await self.check()
                                checked_bytes = len(body)
                        await self.check()  # Includes the final short block and EOF.
                        if not body:
                            raise ValueError("MEME_EMPTY_RESPONSE")
                        mime = response.headers.get("content-type", "").split(";")[0].lower()
                        data = bytes(body)
                        resource = FetchedResource(data, mime, FetchEvidence(
                            requested_url, str(target), tuple(hops),
                            hashlib.sha256(data).hexdigest(), datetime.now(UTC).isoformat(),
                        ))
                        self._issued.append(resource)
                        return resource
                    finally:
                        await response.aclose()
        finally:
            if self.transport is None:
                await delegate.aclose()


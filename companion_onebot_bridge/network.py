import asyncio
import logging
import socket
import time
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect

from .config import private_address
from .onebot_client import http_client

QUIET_LOG = logging.Logger("onebot-private-transport", level=logging.CRITICAL + 1)


@dataclass(frozen=True, repr=False)
class PrivateDestination:
    authority: str
    scheme: str
    hostname: str
    port: int
    address: str


def private_request_matches(request, url, path, *, synthetic=False):
    """Bind original authority/Host/SNI to the checked TCP destination after DNS."""
    original = urlsplit(url)
    expected = httpx.URL(url.rstrip("/") + path)
    pin = request.extensions.get("companion_private_destination")
    if isinstance(pin, PrivateDestination):
        return (
            (pin.authority, pin.scheme, pin.hostname, pin.port)
            == (original.netloc, original.scheme, original.hostname, original.port)
            and private_address(pin.address) and request.url.host == pin.address
            and (not synthetic or ip_address(pin.address).is_loopback)
            and request.headers.get("Host") == original.netloc
            and request.extensions.get("sni_hostname") == original.hostname
            and request.url.copy_with(host=original.hostname) == expected
        )
    # Pure MockTransport is allowed only for the explicitly loopback synthetic route.
    return (synthetic and original.hostname in {"127.0.0.1", "::1", "localhost"}
            and request.url == expected and request.headers.get("Host") == original.netloc)


async def resolve_private(host, port):
    addresses = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), 5
    )
    if not addresses or any(not private_address(item[4][0]) for item in addresses):
        raise ValueError("PUBLIC_DNS_REFUSED")
    return addresses[0][4][0]


class CheckedTransport(httpx.AsyncBaseTransport):
    """One operation's final check, after every request hook (including DNS pinning)."""

    def __init__(self, delegate, check, *, owns_delegate=False):
        self.delegate, self.check, self.owns_delegate = delegate, check, owns_delegate

    async def handle_async_request(self, request):
        await self.check(request)
        from .metrics import HTTP_STARTED, http_finished

        if (not isinstance(self.delegate, CheckedTransport)
            and request.url.path in {"/send_private_msg", "/set_qq_profile", "/set_qq_avatar",
                                     "/send_qzone_msg"}):
            request.extensions[HTTP_STARTED] = time.perf_counter_ns()
        try:
            return await self.delegate.handle_async_request(request)
        except BaseException as error:
            http_finished(request, error)
            raise

    async def aclose(self):
        if self.owns_delegate:
            await self.delegate.aclose()


def checked_http_client(client, check):
    # Borrow the transport, never close it or mutate the caller's hook lifecycle.
    return httpx.AsyncClient(
        base_url=client.base_url, headers=client.headers, timeout=client.timeout,
        follow_redirects=False, trust_env=False,
        event_hooks={key: list(value) for key, value in client.event_hooks.items()},
        transport=CheckedTransport(client._transport, check),
    )


def private_http_client(url, token, *, check=None, transport=None):
    parsed = urlsplit(url)

    async def pin(request):
        # Resolve and validate BEFORE auth leaves this process; pin the actual TCP destination.
        original = httpx.URL(url)
        if (request.url.scheme, request.url.host, request.url.port) != (
            original.scheme, original.host, original.port
        ):
            raise ValueError("PRIVATE_HTTP_AUTHORITY_CHANGED")
        try:
            address = await resolve_private(parsed.hostname, parsed.port)
        except (OSError, TimeoutError):
            # A stopped Compose service has no DNS record. Report it as the transport
            # failure it is, so callers back off or hold input instead of treating a
            # name lookup error as a closed event socket or an unexpected crash.
            raise httpx.ConnectError("PRIVATE_HTTP_RESOLUTION_FAILED",
                                     request=request) from None
        if not private_address(address):
            raise ValueError("PUBLIC_DNS_REFUSED")
        request.headers["Host"] = parsed.netloc
        request.extensions["sni_hostname"] = parsed.hostname
        request.extensions["companion_private_destination"] = PrivateDestination(
            parsed.netloc, parsed.scheme, parsed.hostname, parsed.port, address
        )
        request.url = request.url.copy_with(host=address)

    client = (http_client(url, token) if transport is None
              else http_client(url, token, transport=transport))
    client.event_hooks["request"] = [pin]
    if check is not None:
        client._transport = CheckedTransport(client._transport, check, owns_delegate=True)
    return client


class NoRedirectConnect(connect):
    def process_redirect(self, exc):
        return exc


async def websocket(config, *, synthetic=False, check=None):
    parsed = urlsplit(config.onebot_ws_url)
    address = await resolve_private(parsed.hostname, parsed.port)
    if not private_address(address) or (synthetic and not ip_address(address).is_loopback):
        raise ValueError("WEBSOCKET_DESTINATION_REFUSED")
    if check is not None:
        check()
    connector = NoRedirectConnect(
        config.onebot_ws_url,
        host=address,
        port=parsed.port,
        additional_headers={"Authorization": "Bearer " + config.onebot_token},
        proxy=None,
        max_size=config.max_frame_bytes,
        max_queue=4,
        compression=None,
        open_timeout=10,
        close_timeout=3,
        logger=QUIET_LOG,
    )
    connector.companion_private_destination = address
    return connector


def seal_protocol_content(request, content):
    # HTTPX sends stream, not its content cache. Seal exactly the bounded
    # bytes just checked; never consume an arbitrary replacement stream.
    if "Transfer-Encoding" in request.headers:
        raise ValueError("WINDOW_REQUEST_FRAMING_CHANGED")
    request.stream = httpx.ByteStream(content)
    request.headers["Content-Length"] = str(len(content))

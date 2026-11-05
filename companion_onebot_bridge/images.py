"""Ordered current-owner images, transient URLs, and source-checked bounded GETs."""

import asyncio
import hashlib
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from companion_meme_sources.fetch import global_address, resolve_public

from . import face_graphics
from .spool import encode

HOSTS = frozenset({"multimedia.nt.qq.com.cn", "gchat.qpic.cn", "c2cpicdw.qpic.cn"})
# The authenticated platform message envelope contains at most 64 segments.
MAX_OWNER_IMAGES = 64
_private_download = ContextVar("private_qq_image_download", default=False)


class _PrivateTrace(logging.Filter):
    def filter(self, record):
        return not _private_download.get()


# HTTPCore DEBUG traces include full response headers (including signed Location).
# Filter only this task's download; do not change logger levels or other requests.
for _name in ("connection", "connection_pool", "http11", "http2", "proxy", "socks_proxy"):
    logging.getLogger("httpcore." + _name).addFilter(_PrivateTrace())


@dataclass(frozen=True, repr=False)
class OriginalImage:
    index: int
    slot: int
    source_sha256: str
    url: str
    face_id: str | None = None
    path: tuple[int, ...] = ()
    origin: dict | None = None

    def __post_init__(self):
        from .rich_input import media_location

        media_location(self.index, self.path, self.origin)
        object.__setattr__(self, "path", tuple(self.path))

    def projection(self):
        value = {
            "index": self.index,
            "type": "image",
            "actor": "UNVERIFIED",
            "coverage": "UNREAD",
            "slot": self.slot,
            "source_sha256": self.source_sha256,
        }
        if self.face_id is not None:
            value.update(type="face", face_id=self.face_id)
        if self.path:
            value.update(path=list(self.path), origin=self.origin)
        return value


def image_url(value):
    if (
        not isinstance(value, str)
        or len(value) > 4096
        or "\\" in value
        or any(ord(c) <= 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("IMAGE_URL_NOT_ALLOWED")
    try:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or url.hostname not in HOSTS
            or url.username is not None
            or url.password is not None
            or url.fragment
            or url.port not in {None, 80, 443}
            or not url.path.startswith("/")
        ):
            raise ValueError()
        return url._replace(scheme="https", netloc=url.hostname).geturl()
    except ValueError:
        raise ValueError("IMAGE_URL_NOT_ALLOWED") from None


def original_images(message, *, skip_unsupported=False):
    """Caller authenticated the envelope; never walk node/reply/card descendants.

    Native intake may leave unsupported images in its rich projection while
    selecting only downloadable originals. All other callers remain strict.
    """
    if isinstance(message, str):
        return []
    if not isinstance(message, list) or not 1 <= len(message) <= 64:
        raise ValueError("INVALID_IMAGE_MESSAGE")
    images = [
        (i, value)
        for i, value in enumerate(message)
        if isinstance(value, dict) and (value.get("type") == "image" or (
            value.get("type") == "face" and _face_graphic(value) is not None))
    ]
    originals = []
    for index, value in images:
        try:
            image = _original_image(index, len(originals), value)
        except ValueError as error:
            if (skip_unsupported and value.get("type") == "image" and error.args in {
                ("INVALID_IMAGE_SEGMENT",), ("IMAGE_KIND_UNSUPPORTED",),
                ("IMAGE_URL_NOT_ALLOWED",),
            }):
                continue
            raise
        originals.append(image)
    return originals


def _original_image(index, slot, value):
    if value.get("type") == "face":
        face_id, resource = _face_graphic(value)
        return OriginalImage(index, slot, hashlib.sha256(encode(value).encode()).hexdigest(),
                             resource["url"], face_id)
    data = value.get("data")
    if set(value) != {"type", "data"} or not isinstance(data, dict):
        raise ValueError("INVALID_IMAGE_SEGMENT")
    # SnowLuma emits ordinary image descriptors for subtypes 0/1/7/11.
    # All keep the original CDN bytes and signed upload boundary; flash and
    # market-face carry extra fields and remain unsupported.
    if (
        not set(data) <= {"url", "file", "sub_type", "summary"}
        or type(data.get("sub_type", 0)) is not int
        or data.get("sub_type", 0) not in {0, 1, 7, 11}
        or any(
            not isinstance(data[key], str) or (key != "url" and "\x00" in data[key])
            for key in ("url", "file", "summary") if key in data
        )
    ):
        raise ValueError("IMAGE_KIND_UNSUPPORTED")
    url = image_url(data.get("url") or data.get("file"))
    return OriginalImage(index, slot, hashlib.sha256(encode(value).encode()).hexdigest(), url)


def _face_graphic(value):
    data = value.get("data")
    if not isinstance(data, dict) or set(data) != {"id"}:
        raise ValueError("INVALID_QQ_FACE")
    identifier = str(data["id"]) if type(data["id"]) is int else data["id"]
    resource = face_graphics.info(identifier)
    return (identifier, resource) if resource else None


def source_url(image):
    if image.face_id is None:
        return image_url(image.url)
    resource = face_graphics.info(image.face_id)
    if resource is None or image.url != resource["url"]:
        raise ValueError("QQ_FACE_RESOURCE_CHANGED")
    return image.url


async def _connect_image(delegate, target, pin, check):
    request = httpx.Request(
        "GET", target.copy_with(host=pin),
        headers={"Host": target.host, "Accept-Encoding": "identity"},
        extensions={"sni_hostname": target.host,
                    "timeout": {key: 5.0 for key in ("connect", "read", "write", "pool")}},
    )
    await check()
    dispatched = connect_failed = False

    def destination():
        if (request.method != "GET" or request.url != target.copy_with(host=pin)
            or not global_address(request.url.host) or request.headers.get("Host") != target.host
            or request.extensions.get("sni_hostname") != target.host
            or "Authorization" in request.headers or "Cookie" in request.headers):
            raise ValueError("IMAGE_DESTINATION_CHANGED")

    async def trace(name, info):
        nonlocal dispatched, connect_failed
        if name in {"connection.connect_tcp.failed", "connection.start_tls.failed"}:
            connect_failed = True
        if name.endswith("send_request_headers.started"):
            # Mark before awaiting the guard: once dispatch might have begun,
            # this request can never be repeated, including after a late error.
            dispatched = True
            await check()
            destination()

    request.extensions["trace"] = trace
    destination()
    try:
        return await delegate.handle_async_request(request)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        if dispatched or not connect_failed:
            raise
        # The trusted transport proved TCP/TLS failed before HTTP headers.
        return None


async def fetch_image(image, max_bytes, *, check, transport=None, resolver=resolve_public):
    """The Core check runs after DNS, then actual destination/Host/SNI are rechecked.

    Use the transport directly: HTTPX's client request logger would print signed URLs.
    A proven pre-HTTP connect failure may try another validated address within the
    same ten-second lease. No dispatched GET is repeated; there are no redirects,
    Authorization/Cookie headers or disk writes here.
    """
    if type(max_bytes) is not int or not 1 <= max_bytes <= 4_000_000 or not callable(check):
        raise ValueError("INVALID_IMAGE_DOWNLOAD_GRANT")
    if image.face_id is not None:
        await check()
        source_url(image)
        data = face_graphics.read(image.face_id)
        if len(data) > max_bytes:
            raise ValueError("IMAGE_TOO_LARGE")
        await check()
        return data, "image/png"
    url = image_url(image.url)
    target = httpx.URL(url)
    delegate = transport or httpx.AsyncHTTPTransport(trust_env=False, retries=0)
    response = None
    guard_error = None
    guard_active = False

    async def checked():
        nonlocal guard_error, guard_active
        guard_active = True
        try:
            await check()
        except Exception as error:
            from .capture import capture_diagnostic

            if not hasattr(error, "capture_diagnostic"):
                error.capture_diagnostic = capture_diagnostic(error, "core_check")
            guard_error = error
            raise
        else:
            guard_active = False

    quiet = _private_download.set(True)
    try:
        async with asyncio.timeout(10):
            addresses = await resolver(target.host, 443)
            if not addresses or any(not global_address(ip) for ip in addresses):
                raise ValueError("IMAGE_NONPUBLIC_ADDRESS")
            candidates = addresses[:3] if len(addresses) > 1 else addresses * 2
            for pin in candidates:
                response = await _connect_image(delegate, target, pin, checked)
                if response is not None:
                    break
            if response is None:
                raise ValueError("IMAGE_CONNECTION_FAILED")
            if response.status_code != 200:
                raise ValueError("IMAGE_FETCH_REJECTED")
            if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                raise ValueError("IMAGE_ENCODING_UNSUPPORTED")
            mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif", "image/apng"}:
                raise ValueError("IMAGE_MIME_UNSUPPORTED")
            try:
                size = int(response.headers.get("content-length", "0"))
            except ValueError:
                raise ValueError("IMAGE_SIZE_INVALID") from None
            if not 0 <= size <= max_bytes:
                raise ValueError("IMAGE_TOO_LARGE")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ValueError("IMAGE_TOO_LARGE")
            if not body:
                raise ValueError("IMAGE_EMPTY")
            return bytes(body), mime
    except (httpx.HTTPError, OSError) as error:
        if guard_error is not None:
            raise guard_error from None
        if guard_active:
            from .capture import capture_diagnostic

            error.capture_diagnostic = capture_diagnostic(error, "core_check")
            raise
        code = ("IMAGE_CONNECTION_FAILED" if isinstance(error, (
            httpx.ConnectError, httpx.ConnectTimeout)) else "IMAGE_FETCH_FAILED")
        raise ValueError(code) from None
    finally:
        try:
            if response is not None:
                await response.aclose()
            if transport is None:
                await delegate.aclose()
        finally:
            _private_download.reset(quiet)


async def owner_media_request(runtime, client, path, body=b"", headers=None, *, purpose,
                              before=None, method="POST"):
    async def authorize():
        await runtime.check(client, purpose=purpose)
        if before is not None:
            await before()

    return await _media_request(runtime, client, path, body, headers, purpose=purpose,
                                authorize=authorize, method=method)


async def _media_request(runtime, client, path, body=b"", headers=None, *, purpose,
                         authorize, method="POST"):
    from .event_normalizer import decode
    from .network import checked_http_client, private_request_matches, seal_protocol_content

    expected = body if isinstance(body, bytes) else encode(body).encode()
    if method not in {"GET", "POST"} or method == "GET" and expected:
        raise ValueError("OWNER_MEDIA_REQUEST_INVALID")
    headers = headers or ({"Content-Type": "application/json"} if method == "POST" else {})

    def validate(request):
        if (request.method != method or "Cookie" in request.headers
            or not private_request_matches(request, runtime.config.core_url, path)
            or request.headers.get("Authorization") != "Bearer " + runtime.config.bridge_token
            or request.content != expected
            or any(request.headers.get(key) != value for key, value in headers.items())):
            raise ValueError(purpose.upper() + "_CORE_REQUEST_CHANGED")

    async def boundary(request):
        validate(request)
        await authorize()
        validate(request)
        runtime.verify()
        seal_protocol_content(request, expected)

    try:
        async with checked_http_client(client, boundary) as scoped:
            async with scoped.stream(method, path, content=expected, headers=headers,
                                     timeout=30) as response:
                response.raise_for_status()
                result = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    result.extend(chunk)
                    if len(result) > 65536:
                        raise ValueError("INVALID_CORE_RECEIPT")
    except Exception as error:
        from .capture import capture_diagnostic

        error.capture_diagnostic = capture_diagnostic(error, "core_check")
        raise
    return decode(bytes(result), 65536)


def _image_check_receipt(runtime, receipt, lease_until):
    from .owner_runtime import check_receipt

    if (not isinstance(receipt, dict)
        or set(receipt) != {"approved", "lease_until", "authority_check"}
        or receipt["approved"] is not True or receipt["lease_until"] != lease_until):
        raise ValueError("IMAGE_DOWNLOAD_CHECK_REJECTED")
    result = check_receipt(runtime.config, runtime.authority, "image",
                           receipt["authority_check"], runtime.clock())
    if runtime.legacy and result["budget_journal_id"] != runtime.legacy["budget_journal_id"]:
        raise ValueError("OWNER_LEGACY_BUDGET_CUSTODY_CHANGED")


async def upload_original(runtime, client, event_id, image, *, resume_claim=None,
                          progress=None, transport=None, resolver=resolve_public):
    """Transfer a captured original under its native authority and finite Content lease.

    The caller journals each phase before the corresponding I/O. An uncertain claim
    or upload is reconciled by that caller; this function never retries either.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from companion_delivery_protocol.authority import image_upload_signing_payload

    from .pending_media import _download_claim

    prefix = "/internal/v2/content/" + event_id + "/image"

    async def phase(state, receipt=None):
        if progress is not None:
            await progress(state, receipt)

    async def post(path, body, headers=None, *, before=None):
        return await owner_media_request(runtime, client, path, body, headers,
                                      purpose="image", before=before)

    await runtime.check(client, purpose="image")
    source_url(image)
    claim = resume_claim
    if claim is None:
        await phase("CLAIMING")
        claim = await post(prefix + "/claim", {"request_key": f"image:{image.slot}",
                                               "source_sha256": image.source_sha256})
    _download_claim(claim, runtime, kind="IMAGE")
    await phase("DOWNLOADING", claim)

    async def local_check():
        runtime.verify()

    async def check():
        receipt = await _media_request(runtime, client, prefix + "/check", {
            "lease_token": claim["lease_token"], "source_sha256": image.source_sha256},
            purpose="image", authorize=local_check)
        _image_check_receipt(runtime, receipt, claim["lease_until"])
        if datetime.fromisoformat(claim["lease_until"]) <= runtime.clock():
            raise ValueError("IMAGE_DOWNLOAD_LEASE_EXPIRED")
        runtime.verify()

    data, mime = await fetch_image(image, claim["max_bytes"], check=check,
                                   transport=transport, resolver=resolver)
    await check()
    proof = {"authority": runtime.authority, "event_id": event_id, "slot": image.slot,
             "source_sha256": image.source_sha256, "lease_token": claim["lease_token"],
             "media_sha256": hashlib.sha256(data).hexdigest(), "mime": mime}
    proof["signature"] = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(
        runtime.config.capture_signing_key)).sign(image_upload_signing_payload(proof)).hex()
    await phase("UPLOADING")
    # The later lease check includes the complete current authority receipt.
    # Other media callers keep owner_media_request's independent authority guard.
    result = await _media_request(runtime, client, prefix, data, {"Content-Type": mime,
        "X-Content-Key": f"image:{image.slot}", "X-Content-Lease": claim["lease_token"],
        "X-Content-Proof": encode(proof)}, purpose="image", authorize=check)
    if (set(result) != {"state", "slot", "duplicate", "input_ready"}
        or result["state"] not in {"STAGED", "SUCCEEDED"}
        or type(result["slot"]) is not int or result["slot"] != image.slot
        or type(result["duplicate"]) is not bool or result["input_ready"] is not True):
        raise ValueError("IMAGE_UPLOAD_RECEIPT_REJECTED")
    runtime.verify()
    return result

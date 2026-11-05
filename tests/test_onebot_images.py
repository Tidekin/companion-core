"""No CDN/socket/provider I/O: raw image source fixtures and HTTP MockTransport."""

import asyncio
import logging

import httpx
import pytest

from companion_onebot_bridge import images
from companion_onebot_bridge.window import digest
from tests.conftest import run

URL = "https://multimedia.nt.qq.com.cn/download?appid=1407&rkey=synthetic-private-reference"


def test_qq_cdn_rejects_one_byte_over_32_mib():
    limit = 32 * 1024**2

    async def resolve(*_):
        return ["1.1.1.1"]

    async def check():
        pass

    def get(request):
        return httpx.Response(200, headers={"Content-Type": "image/png",
                                            "Content-Length": str(limit + 1)})

    with pytest.raises(ValueError, match="IMAGE_TOO_LARGE"):
        run(images.fetch_image(images.original_images([image_segment()])[0], limit,
            check=check, resolver=resolve, transport=httpx.MockTransport(get)))


@pytest.mark.parametrize("failure", ["authority", "transport", "storage"])
def test_core_guard_errors_are_not_relabelled_as_download_failure(failure):
    error = (httpx.HTTPStatusError("denied", request=httpx.Request("POST", "https://core/check"),
                                  response=httpx.Response(403)) if failure == "authority" else
             httpx.ReadTimeout("Core unavailable") if failure == "transport" else
             OSError("local custody unavailable"))

    async def resolve(*_):
        return ["1.1.1.1"]

    async def check():
        raise error

    with pytest.raises(type(error)) as caught:
        run(images.fetch_image(images.original_images([image_segment()])[0], 100,
            check=check, resolver=resolve,
            transport=httpx.MockTransport(lambda _: pytest.fail("Guard rejected before GET"))))
    assert caught.value is error


def test_timeout_while_guard_is_suspended_remains_core_check_failure(monkeypatch):
    timeout = asyncio.timeout
    monkeypatch.setattr(images.asyncio, "timeout", lambda _: timeout(0.01))

    async def resolve(*_):
        return ["1.1.1.1"]

    async def check():
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError) as caught:
        run(images.fetch_image(images.original_images([image_segment()])[0], 100,
            check=check, resolver=resolve,
            transport=httpx.MockTransport(lambda _: pytest.fail("Guard did not complete"))))
    assert caught.value.capture_diagnostic.phase == "core_check"


def test_real_httpcore_trace_does_not_log_signed_redirect_header(caplog):
    from httpcore._backends.mock import AsyncMockBackend

    async def scenario():
        async with httpx.AsyncHTTPTransport(trust_env=False, retries=0) as transport:
            transport._pool._network_backend = AsyncMockBackend(
                [
                    b"HTTP/1.1 302 Found\r\nLocation: "
                    + URL.encode()
                    + b"\r\nContent-Length: 0\r\n\r\n",
                ]
            )

            async def resolve(host, port):
                return ["1.1.1.1"]

            async def check():
                return None

            with pytest.raises(ValueError, match="IMAGE_FETCH_REJECTED"):
                await images.fetch_image(
                    images.original_images([image_segment()])[0],
                    100,
                    check=check,
                    resolver=resolve,
                    transport=transport,
                )
            # Filtering is local to the download task, not a global logger disable.
            logging.getLogger("httpcore.http11").debug("public-trace-still-visible")

    with caplog.at_level(logging.DEBUG, logger="httpcore"):
        run(scenario())
    assert "public-trace-still-visible" in caplog.text
    assert URL not in caplog.text and "synthetic-private-reference" not in caplog.text


def image_segment(url=URL, *, subtype=0):
    return {
        "type": "image",
        "data": {"url": url, "file": "synthetic-file", "summary": "", "sub_type": subtype},
    }


@pytest.mark.parametrize("subtype", [0, 1, 7, 11])
def test_top_level_images_keep_source_order_and_hash_without_url_projection(subtype):
    raw = image_segment(subtype=subtype)
    values = images.original_images(
        [
            {"type": "reply", "data": {"id": "42"}},
            {"type": "text", "data": {"text": "owner text"}},
            raw,
            {"type": "node", "data": {"content": [image_segment()]}},
            {"type": "text", "data": {"text": "compare with"}},
            raw,
        ]
    )
    value = values[0]
    assert value.index == 2 and value.source_sha256 == digest(raw)
    assert value.projection() == {
        "index": 2,
        "type": "image",
        "actor": "UNVERIFIED",
        "coverage": "UNREAD",
        "slot": 0,
        "source_sha256": digest(raw),
    }
    assert URL not in repr(value) and URL not in repr(value.projection())
    assert [(image.index, image.slot) for image in values] == [(2, 0), (5, 1)]
    assert values[1].projection()["source_sha256"] == digest(raw)
    assert images.original_images([{"type": "node", "data": {"content": [raw]}}]) == []
    assert images.original_images("[CQ:image,url=forged]") == []
    assert len(images.original_images([raw] * images.MAX_OWNER_IMAGES)) == images.MAX_OWNER_IMAGES
    with pytest.raises(ValueError, match="INVALID_IMAGE_MESSAGE"):
        images.original_images([raw] * (images.MAX_OWNER_IMAGES + 1))


@pytest.mark.parametrize("data", [
    {"sub_type": value} for value in (True, False, "1", 1.0, None, -1, 2, 8, 12)
] + [
    {"type": "flash"}, {"emoji_id": "fake"}, {"emoji_package_id": "fake"},
    {"sub_type": 7, "type": "flash"}, {"sub_type": 7, "emoji_id": "fake"},
    {"key": "fake"}, {"file_size": 100}, {"summary": ["fake"]},
    {"file": {"url": URL}}, {"url": [URL]}, {"file": "file\x00name"},
    {"summary": "summary\x00text"},
])
def test_custom_sticker_keeps_strict_subtype_and_field_validation(data):
    segment = image_segment(subtype=1)
    segment["data"].update(data)
    with pytest.raises(ValueError, match="IMAGE_KIND_UNSUPPORTED"):
        images.original_images([segment])
    assert images.original_images([segment], skip_unsupported=True) == []


@pytest.mark.parametrize("unsupported", [
    {"type": "image", "data": {}},
    {"type": "image", "data": {"url": URL, "sub_type": 999}},
    {"type": "image", "data": {"url": "http://127.0.0.1/private"}},
    {"type": "image", "data": {"url": URL}, "extra": "unknown"},
])
def test_skipping_unsupported_images_preserves_original_indices_and_compacts_slots(unsupported):
    first, last = image_segment(subtype=1), image_segment(subtype=11)
    message = [unsupported, first, {"type": "text", "data": {"text": "owner text"}},
               unsupported, last]
    with pytest.raises(ValueError):
        images.original_images(message)
    retained = images.original_images(message, skip_unsupported=True)
    assert [(image.index, image.slot, image.source_sha256) for image in retained] == [
        (1, 0, digest(first)), (4, 1, digest(last)),
    ]


def test_skipping_images_does_not_skip_invalid_face_or_unexpected_internal_errors(monkeypatch):
    with pytest.raises(ValueError, match="INVALID_QQ_FACE"):
        images.original_images([{"type": "face", "data": {"id": "14", "extra": True}}],
                               skip_unsupported=True)
    with pytest.raises(ValueError, match="INVALID_IMAGE_MESSAGE"):
        images.original_images([image_segment()] * 65, skip_unsupported=True)

    def changed(*_):
        raise ValueError("IMAGE_SOURCE_CHANGED")

    monkeypatch.setattr(images, "_original_image", changed)
    with pytest.raises(ValueError, match="IMAGE_SOURCE_CHANGED"):
        images.original_images([image_segment(subtype=1)], skip_unsupported=True)


@pytest.mark.parametrize("subtype", [0, 1, 7, 11])
@pytest.mark.parametrize(
    "url",
    [
        "https://multimedia.nt.qq.com.cn.evil.invalid/x",
        "file:///private",
        "https://multimedia.nt.qq.com.cn@127.0.0.1/x",
        "https://user:secret@gchat.qpic.cn/x",
        "https://gchat.qpic.cn:444/x",
        "https://gchat.qpic.cn/x#secret",
        "https://gchat.qpic.cn\\@evil.invalid/x",
        "https://gchat.qpic.cn/x\x00secret",
        "https://127.0.0.1/x",
        "data:image/png;base64,private",
    ],
)
def test_unknown_or_confused_image_destinations_are_rejected_before_io(url, subtype):
    with pytest.raises(ValueError, match="IMAGE_URL_NOT_ALLOWED"):
        images.original_images([image_segment(url, subtype=subtype)])


@pytest.mark.parametrize(
    "addresses",
    [[], ["127.0.0.1"], ["1.1.1.1", "10.0.0.1"], ["::1"], ["224.0.0.1"], ["::ffff:127.0.0.1"]],
)
@pytest.mark.parametrize("subtype", [0, 1, 7, 11])
def test_dns_private_or_mixed_answers_never_get(addresses, subtype):
    calls = []

    async def resolve(host, port):
        return addresses

    async def check():
        calls.append("check")

    def get(request):
        calls.append("GET")
        return httpx.Response(200, content=b"fake", headers={"content-type": "image/png"})

    with pytest.raises(ValueError, match="IMAGE_NONPUBLIC_ADDRESS"):
        run(
            images.fetch_image(
                images.original_images([image_segment(subtype=subtype)])[0],
                100,
                check=check,
                resolver=resolve,
                transport=httpx.MockTransport(get),
            )
        )
    assert calls == []


@pytest.mark.parametrize(
    "failure", [None, "source", "redirect", "bytes", "mime", "encoding", "error"]
)
@pytest.mark.parametrize("subtype", [0, 1, 7, 11])
def test_single_bounded_get_is_pinned_source_checked_and_never_logs_url(caplog, failure, subtype):
    calls = []
    caplog.set_level(logging.DEBUG)

    async def resolve(host, port):
        assert (host, port) == ("multimedia.nt.qq.com.cn", 443)
        calls.append("DNS")
        return ["1.1.1.1"]

    async def check():
        calls.append("check")
        if failure == "source":
            raise ValueError("IMAGE_SOURCE_CHANGED")

    def get(request):
        calls.append("GET")
        assert request.url.host == "1.1.1.1"
        assert request.headers["Host"] == "multimedia.nt.qq.com.cn"
        assert request.extensions["sni_hostname"] == "multimedia.nt.qq.com.cn"
        assert "Authorization" not in request.headers and "Cookie" not in request.headers
        if failure == "error":
            raise httpx.ConnectError(URL)
        headers = {"Content-Type": "image/png"}
        if failure == "mime":
            headers["Content-Type"] = "text/html"
        if failure == "encoding":
            headers["Content-Encoding"] = "gzip"
        return httpx.Response(
            302 if failure == "redirect" else 200,
            headers=headers,
            content=b"0123456789" if failure == "bytes" else b"fake",
        )

    invocation = images.fetch_image(
        images.original_images([image_segment(subtype=subtype)])[0],
        5,
        check=check,
        resolver=resolve,
        transport=httpx.MockTransport(get),
    )
    if failure:
        with pytest.raises(ValueError) as error:
            run(invocation)
        assert URL not in str(error.value)
    else:
        assert run(invocation) == (b"fake", "image/png")
    assert calls == (["DNS", "check"] if failure == "source" else ["DNS", "check", "GET"])
    assert URL not in caplog.text and "synthetic-private-reference" not in caplog.text


# Production-shaped answer: IPv6 strings sort before "43.", so a sorted list began with three
# unroutable IPv6 pins and every attempt failed on a host without IPv6.
V6 = ["240d:c000:2010:1a1f:0:9ee4:42f8:b4e4", "240d:c000:2010:1a1f:0:9ee4:42fe:a3a4",
      "240d:c000:2010:1a1f:0:9ee4:4303:ce06"]
V4 = ["43.128.17.103", "43.129.115.219"]


def answer(monkeypatch, order):
    import socket

    def getaddrinfo(host, port, *args, **kwargs):
        assert (host, port) == ("multimedia.nt.qq.com.cn", 443)
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (ip, port)) for ip in order]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize("order,expected", [
    (V6 + V4, [V6[0], V4[0], V6[1], V4[1], V6[2]]),
    (V4 + V6 + V4[:1], [V4[0], V6[0], V4[1], V6[1], V6[2]]),
])
def test_resolver_keeps_system_order_and_interleaves_families(monkeypatch, order, expected):
    answer(monkeypatch, order)
    assert run(images.resolve_public("multimedia.nt.qq.com.cn", 443)) == expected


def test_unroutable_ipv6_falls_back_to_ipv4_within_one_download(monkeypatch):
    import ssl

    import httpcore

    answer(monkeypatch, V6 + V4)
    attempts = []

    class Stream(httpcore.AsyncMockStream):
        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):  # noqa: ASYNC109
            assert server_hostname == "multimedia.nt.qq.com.cn"
            return self

    class Backend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, host, port, **kwargs):
            attempts.append(host)
            if ":" in host:
                raise httpcore.ConnectError("[Errno 101] Network is unreachable")
            return Stream([b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n"
                           b"Content-Length: 4\r\n\r\nfake"])

    transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0)
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), network_backend=Backend(),
        max_keepalive_connections=0, retries=0)

    async def check():
        return None

    assert run(images.fetch_image(images.original_images([image_segment()])[0], 100,
                                  check=check, transport=transport)) == (b"fake", "image/png")
    assert attempts == [V6[0], V4[0]]

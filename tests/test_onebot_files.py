"""Pinned native-shaped fixtures and mock HTTP only; no live QQ acceptance claim.

SnowLuma db142db9e6398c779283eab9afc3552da1e2c361 primary sources:
https://github.com/SnowLuma/SnowLuma/blob/db142db9e6398c779283eab9afc3552da1e2c361/packages/onebot/src/event-converter/element-codecs.ts#L497-L519
https://github.com/SnowLuma/SnowLuma/blob/db142db9e6398c779283eab9afc3552da1e2c361/packages/onebot/src/actions/group-file.ts#L329-L357
https://github.com/SnowLuma/SnowLuma/blob/db142db9e6398c779283eab9afc3552da1e2c361/packages/protocol/src/oidb-services/group-file/get-private-file-url.ts#L49-L56
"""  # noqa: E501

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest

from companion_onebot_bridge import files
from companion_onebot_bridge.network import PrivateDestination
from companion_onebot_bridge.onebot_client import OneBotClient, http_client
from companion_onebot_bridge.window import digest
from tests.conftest import run
from tests.support.onebot_events import config, raw_event
from tests.support.onebot_files import BODY, URL, public, segment


def original(**changes):
    return files.original_files([segment(**changes)])[0]


async def valid():
    pass


@asynccontextmanager
async def protocol(handler=None, *, hooks=()):
    cfg = config(onebot_http_url="http://127.0.0.1:3000", onebot_ws_url="ws://127.0.0.1:3001")

    def default(request):
        assert request.url.path == files.ACTION
        assert json.loads(request.content) == {
            "user_id": 10001, "file_id": "opaque-file-1", "file_hash": "opaque-hash",
        }
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"url": URL}})

    async with http_client(cfg.onebot_http_url, cfg.onebot_token,
                           transport=httpx.MockTransport(handler or default)) as client:
        client.event_hooks["request"] = list(hooks)
        yield OneBotClient(cfg, client)


def response(body=BODY, **headers):
    return httpx.Response(200, content=body, headers={"Content-Type": "text/plain", **headers})


async def download(*, file=None, limit=1024, check=valid, action=None, get=None,
                   resolver=public, **options):
    async with protocol(action) as onebot:
        return await files.fetch_file(file or original(), limit, onebot=onebot, check=check,
            transport=httpx.MockTransport(get or (lambda request: response())),
            resolver=resolver, synthetic=True, **options)


def test_real_segment_shape_keeps_order_source_hash_and_no_url_in_projection():
    raw = segment()
    message = [{"type": "text", "data": {"text": "read these"}}, raw,
               {"type": "node", "data": {"content": [raw]}},
               {"type": "reply", "data": {"id": "13", "message": [raw]}}, raw]
    first, second = files.original_files(message)
    assert (first.index, first.slot, second.index, second.slot) == (1, 0, 4, 1)
    assert first.source_sha256 == second.source_sha256 == digest(raw)
    assert first.projection() == {
        "index": 1, "type": "file", "actor": "UNVERIFIED", "coverage": "UNREAD", "slot": 0,
        "source_sha256": digest(raw), "file_id": "opaque-file-1", "filename": "private.txt",
        "size": len(BODY),
    }
    assert first.file_hash == "opaque-hash"
    assert "private.txt" not in repr(first)
    assert "127.0.0.1" not in json.dumps(first.projection())
    assert files.original_files("[CQ:file,file_id=forged]") == []
    assert files.original_files([{"type": "node", "data": {"content": [raw]}}]) == []
    assert len(files.original_files([raw] * 64)) == 64
    with pytest.raises(ValueError, match="INVALID_FILE_MESSAGE"):
        files.original_files([raw] * 65)


@pytest.mark.parametrize("change", [
    {"post_type": "notice"}, {"post_type": "message_sent"}, {"message_type": "group"},
    {"sub_type": "group"}, {"sub_type": "other"}, {"user_id": 99999}, {"self_id": 99999},
    {"sender": {"user_id": 99999}}, {"group_id": 123},
    {"sender": {"user_id": 10001, "group_id": 123}},
])
def test_non_owner_or_non_private_frames_produce_no_file_sources(change):
    raw = raw_event(message=[segment()], **change)
    assert files.owner_files(json.dumps(raw), config(), now=raw["time"]) == []


def test_owner_frame_requires_existing_identity_time_and_shape_validation():
    raw = raw_event(message=[segment()])
    assert files.owner_files(json.dumps(raw), config(), now=raw["time"])[0] == original()
    raw["time"] = True
    with pytest.raises(ValueError, match="INVALID_EVENT_TIME"):
        files.owner_files(json.dumps(raw), config())


@pytest.mark.parametrize("changes,code", [
    ({"name": "changed.txt"}, "FILE_ALIAS_MISMATCH"),
    ({"id": "changed"}, "FILE_ALIAS_MISMATCH"),
    ({"size": 1}, "FILE_ALIAS_MISMATCH"),
    ({"size": True, "file_size": True}, "INVALID_FILE_SIZE"),
    ({"size": -1, "file_size": -1}, "INVALID_FILE_SIZE"),
    ({"size": 2**53, "file_size": 2**53}, "INVALID_FILE_SIZE"),
    ({"size": None, "file_size": None}, "INVALID_FILE_SIZE"),
    ({"file_id": "", "id": ""}, "INVALID_FILE_SEGMENT"),
    ({"file_id": "x" * 513, "id": "x" * 513}, "INVALID_FILE_SEGMENT"),
    ({"file_id": "a\x00b", "id": "a\x00b"}, "INVALID_FILE_SEGMENT"),
    ({"file": "../secret.txt", "name": "../secret.txt"}, "INVALID_FILE_SEGMENT"),
    ({"file": "C:secret.txt", "name": "C:secret.txt"}, "INVALID_FILE_SEGMENT"),
    ({"file": "x\\y.txt", "name": "x\\y.txt"}, "INVALID_FILE_SEGMENT"),
    ({"file": "x\n.txt", "name": "x\n.txt"}, "INVALID_FILE_SEGMENT"),
    ({"file": "x\x85.txt", "name": "x\x85.txt"}, "INVALID_FILE_SEGMENT"),
    ({"file": "x" * 256, "name": "x" * 256}, "INVALID_FILE_SEGMENT"),
    ({"raw_action": "get_file"}, "INVALID_FILE_SEGMENT"),
    ({"file_hash": "x" * 1025}, "INVALID_FILE_SEGMENT"),
])
def test_bad_metadata_is_rejected_without_truncation_or_silent_renaming(changes, code):
    with pytest.raises(ValueError, match=code):
        original(**changes)


def test_legacy_aliases_unknown_size_and_source_only_url_are_bound():
    raw = {"type": "file", "data": {"name": "notes.md", "id": "opaque-id"}}
    value = files.original_files([raw])[0]
    assert value.size is None and value.source_sha256 == digest(raw)
    assert original(file_size=0, size=0).size is None
    assert original(url="https://arbitrary.invalid/not-an-authority").source_sha256 != (
        original().source_sha256)


def test_native_lookup_body_and_pinned_get_produce_hashed_original_metadata(caplog):
    calls = []

    async def check():
        calls.append("check")

    def get(request):
        calls.append("GET")
        assert str(request.url).startswith("http://1.1.1.1:8080/ftn_handler/")
        assert request.headers["Host"] == "download.ftn.qq.com:8080"
        assert request.headers["Accept-Encoding"] == "identity"
        assert request.extensions["sni_hostname"] == "download.ftn.qq.com"
        assert "Authorization" not in request.headers and "Cookie" not in request.headers
        return response(**{"Content-Type": "Text/Plain; charset=utf-8",
                           "Content-Disposition": 'attachment; filename="changed.exe"'})

    caplog.set_level(logging.DEBUG)
    result = run(download(check=check, get=get))
    assert result.body == BODY and result.original == original()
    assert result.mime == "text/plain" and result.media_sha256 == hashlib.sha256(BODY).hexdigest()
    assert result.metadata() == {
        "slot": 0, "source_sha256": original().source_sha256, "file_id": "opaque-file-1",
        "filename": "private.txt", "size": len(BODY), "bytes": len(BODY),
        "mime": "text/plain", "media_sha256": hashlib.sha256(BODY).hexdigest(),
    }
    assert calls == ["check", "check", "check", "check", "GET", "check", "check"]
    assert URL not in caplog.text and "synthetic-signed-file" not in repr(result)
    assert "untrusted" not in repr(result)


def native_url(url):
    return lambda request: httpx.Response(
        200, json={"status": "ok", "retcode": 0, "data": {"url": url}})


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "data:application/pdf,secret", "ftp://example.com/private",
    "https://user:secret@example.com/path", "https://example.com/path#fragment",
    "https://example.com/path#", "https://example.com:0/path", "http://example.com:65536/x",
    "http://example.com\\@127.0.0.1/path", "http://example.com/a b",
    "http://example.com/a\x00b", "http://example.com//path", "http://example.com/a/../b",
    "http://example.com", "", None, 123,
])
def test_bad_native_urls_are_rejected_before_dns_or_download(url):
    async def never_resolve(host, port):
        pytest.fail("bad URL reached DNS")

    with pytest.raises(ValueError, match="FILE_URL_NOT_ALLOWED"):
        run(download(action=native_url(url), resolver=never_resolve,
                     get=lambda request: pytest.fail("bad URL reached GET")))


@pytest.mark.parametrize("data", [None, {}, {"file": URL}, {"url": URL, "filename": "new.txt"}])
def test_native_response_is_exact_url_receipt(data):
    action = lambda request: httpx.Response(  # noqa: E731
        200, json={"status": "ok", "retcode": 0, "data": data})
    with pytest.raises(ValueError, match="FILE_URL_RESOLUTION_FAILED"):
        run(download(action=action, get=lambda request: pytest.fail("invalid receipt")))


@pytest.mark.parametrize("body", [
    {"status": "failed", "retcode": 100, "data": {"url": URL}},
    {"status": "ok", "retcode": False, "data": {"url": URL}},
])
def test_failed_native_action_does_not_use_message_url(body):
    with pytest.raises(ValueError, match="FILE_URL_RESOLUTION_FAILED"):
        run(download(action=lambda request: httpx.Response(200, json=body),
                     get=lambda request: pytest.fail("invalid action")))


@pytest.mark.parametrize("addresses", [
    [], ["127.0.0.1"], ["10.1.2.3"], ["169.254.169.254"], ["::1"], ["fc00::1"],
    ["224.0.0.1"], ["::ffff:1.1.1.1"], ["1.1.1.1", "192.168.1.1"],
])
def test_dns_private_mixed_or_transition_answers_never_download(addresses):
    async def resolver(host, port):
        return addresses

    with pytest.raises(ValueError, match="FILE_NONPUBLIC_ADDRESS"):
        run(download(resolver=resolver, get=lambda request: pytest.fail("private GET")))


@pytest.mark.parametrize("url", ["http://127.0.0.1:7000/x", "https://[::1]:443/x",
                                  "http://169.254.169.254:80/x"])
def test_native_literal_private_ip_never_downloads_even_in_synthetic_mode(url):
    with pytest.raises(ValueError, match="FILE_NONPUBLIC_ADDRESS"):
        run(download(action=native_url(url), get=lambda request: pytest.fail("private GET")))


@pytest.mark.parametrize("url,host,authority,port", [
    ("http://1.1.1.1:8088/file?sig=private", "1.1.1.1", "1.1.1.1:8088", 8088),
    ("https://[2606:4700:4700::1111]:8443/file", "2606:4700:4700::1111",
     "[2606:4700:4700::1111]:8443", 8443),
])
def test_native_public_ip_and_port_are_pinned_without_dns(url, host, authority, port):
    async def never_resolve(host, port):
        pytest.fail("literal IP must not use DNS")

    def get(request):
        assert request.url.host == host and request.url.port == port
        assert request.headers["Host"] == authority
        assert request.extensions["sni_hostname"] == host
        return response()

    assert run(download(action=native_url(url), resolver=never_resolve, get=get)).body == BODY


@pytest.mark.parametrize("url,port", [("http://download.ftn.qq.com:80/file", 80),
                                     ("https://download.ftn.qq.com/file", 443)])
def test_native_default_ports_keep_dns_and_host_consistent(url, port):
    async def resolver(host, actual_port):
        assert host == "download.ftn.qq.com" and actual_port == port
        return ["1.1.1.1"]

    def get(request):
        assert request.headers["Host"] == "download.ftn.qq.com"
        return response()

    assert run(download(action=native_url(url), resolver=resolver, get=get)).body == BODY


def test_core_claim_may_raise_byte_limit_to_configured_128_mib():
    assert run(download(limit=128 * 1024 * 1024)).body == BODY
    with pytest.raises(ValueError, match="INVALID_FILE_DOWNLOAD_GRANT"):
        run(download(limit=128 * 1024 * 1024 + 1))


@pytest.mark.parametrize("options", [{"allowed_hosts": {"other.qq.com"}},
                                      {"allowed_ports": {443}}])
def test_operator_can_narrow_native_endpoint_grant(options):
    with pytest.raises(ValueError, match="FILE_URL_NOT_ALLOWED"):
        run(download(**options))


@pytest.mark.parametrize("options", [{"allowed_hosts": "download.ftn.qq.com"},
                                      {"allowed_ports": "8080"}, {"allowed_ports": [True]}])
def test_endpoint_policy_requires_explicit_exact_host_and_port_collections(options):
    with pytest.raises(ValueError, match="INVALID_FILE_DOWNLOAD_GRANT"):
        run(download(action=lambda request: pytest.fail("invalid grant RPC"), **options))


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_are_not_followed(status):
    calls = []

    def get(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "http://127.0.0.1/private"})

    with pytest.raises(ValueError, match="FILE_REDIRECT_REFUSED"):
        run(download(get=get))
    assert len(calls) == 1


@pytest.mark.parametrize("reply,code", [
    (httpx.Response(404), "FILE_FETCH_REJECTED"),
    (response(body=b"", **{"Content-Encoding": "gzip"}), "FILE_ENCODING_UNSUPPORTED"),
    (response(**{"Content-Type": "text/html"}), "FILE_MIME_UNSUPPORTED"),
    (response(**{"Content-Length": "-1"}), "FILE_SIZE_INVALID"),
    (response(**{"Content-Length": "x"}), "FILE_SIZE_INVALID"),
    (response(**{"Content-Length": "1, 1"}), "FILE_SIZE_INVALID"),
    (response(**{"Content-Length": "2048"}), "FILE_TOO_LARGE"),
    (response(**{"Content-Length": "1"}), "FILE_SIZE_MISMATCH"),
    (response(body=b"short"), "FILE_SIZE_MISMATCH"),
    (response(body=b""), "FILE_EMPTY"),
    (response(body=b"x" * 1025), "FILE_TOO_LARGE"),
])
def test_http_size_encoding_mime_and_status_failures_are_explicit(reply, code):
    with pytest.raises(ValueError, match=code):
        run(download(get=lambda request: reply))


def test_declared_source_over_limit_does_not_resolve_native_url():
    with pytest.raises(ValueError, match="FILE_TOO_LARGE"):
        run(download(limit=4, action=lambda request: pytest.fail("oversize RPC")))


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, callback=lambda: None):
        self.chunks, self.callback, self.closed = chunks, callback, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        self.callback()

    async def aclose(self):
        self.closed = True


def test_streamed_unstated_size_is_bounded_without_content_length():
    stream = Chunks([b"a" * 3, b"b" * 3])
    with pytest.raises(ValueError, match="FILE_TOO_LARGE"):
        run(download(file=original(file_size=0, size=0), limit=5,
                     get=lambda request: httpx.Response(200, stream=stream)))
    assert stream.closed


@pytest.mark.parametrize("revoke_at", ["before_action", "after_action", "after_dns",
                                       "before_send", "after_headers", "after_body", "on_close"])
def test_recall_or_source_revocation_never_returns_bytes(revoke_at):
    revoked, calls = False, []

    def revoke():
        nonlocal revoked
        revoked = True

    async def check():
        if revoked or revoke_at == "before_action":
            raise ValueError("FILE_SOURCE_RECALLED")

    def action(request):
        calls.append("action")
        if revoke_at == "after_action":
            revoke()
        return native_url(URL)(request)

    async def resolver(host, port):
        if revoke_at == "after_dns":
            revoke()
        return ["1.1.1.1"]

    async def get(request):
        if revoke_at == "before_send":
            revoke()
            await request.extensions["trace"]("http11.send_request_headers.started", {})
        calls.append("GET")
        if revoke_at == "after_headers":
            revoke()
        class ClosingChunks(Chunks):
            async def aclose(self):
                await super().aclose()
                if revoke_at == "on_close":
                    revoke()

        return httpx.Response(200, stream=ClosingChunks(
            [BODY], revoke if revoke_at == "after_body" else lambda: None))

    with pytest.raises(ValueError, match="FILE_SOURCE_RECALLED"):
        run(download(check=check, action=action, get=get, resolver=resolver))
    if revoke_at in {"before_action", "after_action", "after_dns", "before_send"}:
        assert "GET" not in calls


@pytest.mark.parametrize("change", ["id", "path", "auth", "cookie", "destination"])
def test_onebot_request_hooks_cannot_change_the_action_after_source_check(change):
    async def mutate(request):
        if change == "id":
            request._content = b'{"user_id":10001,"file_id":"other","file_hash":"opaque-hash"}'
        elif change == "path":
            request.url = request.url.copy_with(path="/send_private_msg")
        elif change == "auth":
            request.headers["Authorization"] = "Bearer changed"
        elif change == "cookie":
            request.headers["Cookie"] = "secret"
        else:
            request.url = request.url.copy_with(host="127.0.0.2")

    async def scenario():
        async with protocol(lambda request: pytest.fail("mutated native request"),
                            hooks=[mutate]) as onebot:
            await files.fetch_file(original(), 1024, onebot=onebot, check=valid,
                                   synthetic=True, transport=httpx.MockTransport(response))

    with pytest.raises(ValueError, match="FILE_ACTION_CHANGED"):
        run(scenario())


def test_real_private_onebot_pin_is_accepted_without_synthetic_bypass():
    async def pin(request):
        request.extensions["companion_private_destination"] = PrivateDestination(
            "127.0.0.1:3000", "http", "127.0.0.1", 3000, "127.0.0.1")
        request.extensions["sni_hostname"] = "127.0.0.1"

    async def scenario():
        async with protocol(hooks=[pin]) as onebot:
            return await files.fetch_file(original(), 1024, onebot=onebot, check=valid,
                resolver=public, transport=httpx.MockTransport(lambda request: response()))

    assert run(scenario()).body == BODY


def test_native_action_seals_checked_content_over_a_replaced_request_stream():
    async def replace_stream(request):
        request.stream = httpx.ByteStream(b'{"file_id":"unrelated-private-file"}')

    async def native(request):
        wire = b"".join([part async for part in request.stream])
        assert json.loads(wire) == {
            "user_id": 10001, "file_id": "opaque-file-1", "file_hash": "opaque-hash"}
        assert request.headers["Content-Length"] == str(len(wire))
        return native_url(URL)(request)

    async def scenario():
        async with protocol(native, hooks=[replace_stream]) as onebot:
            return await files.fetch_file(original(), 1024, onebot=onebot, check=valid,
                resolver=public, synthetic=True,
                transport=httpx.MockTransport(lambda request: response()))

    assert run(scenario()).body == BODY


@pytest.mark.parametrize("change", ["method", "host", "sni", "auth", "cookie", "port"])
def test_cdn_final_send_trace_rechecks_the_actual_request(change):
    async def get(request):
        if change == "method":
            request.method = "POST"
        elif change == "host":
            request.headers["Host"] = "other.example"
        elif change == "sni":
            request.extensions["sni_hostname"] = "other.example"
        elif change == "auth":
            request.headers["Authorization"] = "Bearer secret"
        elif change == "cookie":
            request.headers["Cookie"] = "secret"
        else:
            request.url = request.url.copy_with(port=9000)
        await request.extensions["trace"]("http11.send_request_headers.started", {})
        pytest.fail("modified request crossed final check")

    with pytest.raises(ValueError, match="FILE_DESTINATION_CHANGED"):
        run(download(get=get))


def test_connect_failure_is_never_retried_and_does_not_expose_signed_url():
    calls = []

    def get(request):
        calls.append(request)
        raise httpx.ConnectError(URL)

    with pytest.raises(ValueError, match="FILE_CONNECTION_FAILED") as error:
        run(download(get=get))
    assert len(calls) == 1 and URL not in str(error.value)


def test_total_timeout_sanitizes_url_and_leaves_no_retry(monkeypatch):
    monkeypatch.setattr(files, "TIMEOUT_SECONDS", 0.01)

    async def get(request):
        await asyncio.sleep(1)

    with pytest.raises(ValueError, match="FILE_FETCH_TIMEOUT"):
        run(download(get=get))


@pytest.mark.parametrize("at", [1, 4, 6])
@pytest.mark.parametrize("failure", ["status", "timeout", "io"])
def test_core_guard_error_is_not_reclassified_as_a_file_download_failure(at, failure):
    request = httpx.Request("POST", "http://127.0.0.1/core-check")
    error = {
        "status": httpx.HTTPStatusError("denied", request=request,
                                         response=httpx.Response(403, request=request)),
        "timeout": httpx.ReadTimeout("core check timeout", request=request),
        "io": OSError("local source custody failed"),
    }[failure]
    checks = 0

    async def check():
        nonlocal checks
        checks += 1
        if checks == at:
            raise error

    with pytest.raises(type(error)) as caught:
        run(download(check=check))
    assert caught.value is error
    assert checks == at


def test_file_total_timeout_inside_core_guard_stays_a_core_check_error(monkeypatch):
    from companion_onebot_bridge.capture import diagnostic_fields

    monkeypatch.setattr(files, "TIMEOUT_SECONDS", 0.01)

    async def waiting_check():
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError) as caught:
        run(download(check=waiting_check))
    assert diagnostic_fields(caught.value) == {
        "capture_phase": "core_check", "capture_reason": "CAPTURE_TIMEOUT"}


def test_real_httpcore_signed_redirect_trace_is_private(caplog):
    from httpcore._backends.mock import AsyncMockBackend

    async def scenario():
        async with protocol() as onebot:
            async with httpx.AsyncHTTPTransport(trust_env=False, retries=0) as transport:
                transport._pool._network_backend = AsyncMockBackend([
                    b"HTTP/1.1 302 Found\r\nLocation: " + URL.encode()
                    + b"\r\nContent-Length: 0\r\n\r\n",
                ])
                with pytest.raises(ValueError, match="FILE_REDIRECT_REFUSED"):
                    await files.fetch_file(original(), 1024, onebot=onebot, check=valid,
                        transport=transport, resolver=public, synthetic=True)
        logging.getLogger("httpcore.http11").debug("public trace still visible")

    with caplog.at_level(logging.DEBUG, logger="httpcore"):
        run(scenario())
    assert "public trace still visible" in caplog.text and URL not in caplog.text


def test_corrupt_original_manifest_is_rejected_before_io():
    with pytest.raises(ValueError, match="INVALID_FILE_SEGMENT"):
        replace(original(), source_sha256="not-a-hash")

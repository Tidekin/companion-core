"""One authorized Bili media download, local extraction, and immutable Core receipts.

WBI query signing follows the vendor permutation also documented by yt-dlp's
BilibiliBaseIE at e8de28e23c1ecb4a12b2c3dec188c07e998c412c. No client fingerprint
fabrication, access-denial fallback, alternate CDN, or automatic I/O replay.
"""

import asyncio
import hashlib
import math
import os
import re
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpcore
import httpx

from companion.bili_video_protocol import (
    MAX_METADATA_BYTES,
    assets_to_wire,
    exact,
    json_sha256,
    prepare_response,
    upload_response,
)
from companion.video_media_contract import ClipRange, DownloadedMedia
from source_reader.bili_signing import signed_query
from source_reader.bilibili import api_data
from source_reader.network import ReadBudget, ReadFailure
from source_reader.reading import ReadingStopped
from source_reader.video_extract import MediaExtractionError, extract_media

API_ORIGIN = "https://api.bilibili.com"
API_PATHS = {"/x/web-interface/view", "/x/web-interface/nav", "/x/player/wbi/playurl"}
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
def media_locator(value, *, audio=False):
    if (not isinstance(value, str) or len(value) > 4096 or "\\" in value
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        raise ReadFailure("MEDIA_LOCATOR_NOT_ALLOWED")
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.port not in {None, 443}
        or parsed.username is not None or parsed.password is not None or parsed.fragment
        or not re.fullmatch(r"[a-z0-9.-]+", host)
        or not any(host == suffix or host.endswith("." + suffix)
                   for suffix in (("bilivideo.com", "bilivideo.cn") if audio
                                  else ("bilivideo.com",)))
        or not parsed.path.startswith("/")):
        raise ReadFailure("MEDIA_LOCATOR_NOT_ALLOWED")
    return value


def representation(data, limits):
    rows = (data.get("dash") or {}).get("video")
    if not isinstance(rows, list) or len(rows) > 128:
        raise ReadFailure("MEDIA_REPRESENTATION_UNAVAILABLE")
    candidates = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        mime, codec = row.get("mimeType", row.get("mime_type")), row.get("codecs")
        if not isinstance(codec, str) or not (
            mime == "video/mp4" and re.fullmatch(r"avc1\.[0-9A-Fa-f]{6}", codec)
            or mime == "video/webm" and re.fullmatch(r"(?:vp8|vp9|vp09(?:\.[0-9]{2}){0,8})", codec)
        ):
            continue
        width, height, bandwidth, size = (row.get(key) for key in (
            "width", "height", "bandwidth", "size",
        ))
        if (type(width) is not int or type(height) is not int or width < 1 or height < 1
            or width * height > limits.max_source_pixels
            or type(bandwidth) is not int or bandwidth <= 0
            or size is not None and (
                type(size) is not int or not 1 <= size <= limits.max_input_bytes)
            or math.ceil(bandwidth * data["timelength"] / 8000) > limits.max_input_bytes):
            continue
        locator = row.get("baseUrl", row.get("base_url", row.get("url")))
        try:
            media_locator(locator)
        except (ValueError, ReadFailure):
            continue
        candidates.append((bandwidth, width * height, index, row, locator))
    if not candidates:
        raise ReadFailure("MEDIA_REPRESENTATION_UNAVAILABLE")
    *_, row, locator = min(candidates, key=lambda value: value[:3])
    return row, locator


class MediaHTTP:
    """Uses the Reader's pinned public transport; cookies only on three fixed APIs."""

    def __init__(self, public, budget, guard, *, audio=False):
        self.public, self.budget, self.guard = public, budget, guard
        self.audio = audio

    def account(self):
        verify = getattr(self.public, "verify", None)
        return verify(self.budget) if verify else None

    def denied(self, error):
        if str(error) in {"UPSTREAM_ACCESS_LIMIT", "SOURCE_CREDENTIAL_REVOKED"}:
            if hasattr(self.public, "blocked_grant"):
                self.public.blocked_grant = self.budget.authorization

    async def transfer(self, url, *, maximum, sink=None, mime=None):
        if type(maximum) is not int or maximum <= 0:
            raise ReadFailure("READER_BYTE_LIMIT")
        await self.guard()
        grant = self.account()
        parsed = urlsplit(url)
        api = (parsed.scheme == "https" and parsed.netloc == "api.bilibili.com"
               and parsed.path in API_PATHS and not parsed.fragment)
        if not api:
            media_locator(url, audio=self.audio)
        headers = {"Accept-Encoding": "identity", "User-Agent": "CompanionSourceReader/0.6",
                   "Referer": "https://www.bilibili.com/"}
        if api and grant:
            headers["Cookie"] = "; ".join(f"{key}={value}"
                                           for key, value in grant["cookies"].items())
        self.budget.reserve()
        try:
            async with self.public.pool.stream(
                "GET", url, headers=headers,
                extensions=self.budget.request_extensions(self.account),
            ) as response:
                info = {key.decode("ascii").lower(): value.decode("latin1")
                        for key, value in response.headers}
                if sum(len(key) + len(value) for key, value in response.headers) > 8192:
                    raise ReadFailure("READER_BYTE_LIMIT")
                if response.status in {401, 403, 412, 429}:
                    raise ReadFailure("UPSTREAM_ACCESS_LIMIT")
                if response.status != 200:
                    raise ReadFailure("UPSTREAM_HTTP_ERROR")
                if "content-range" in info:
                    raise ReadFailure("MEDIA_BODY_INCOMPLETE")
                if info.get("content-encoding", "identity").strip().lower() != "identity":
                    raise ReadFailure("READER_ENCODING_UNSUPPORTED")
                if mime and info.get("content-type", mime).split(";", 1)[0].strip() not in {
                    mime, "application/octet-stream",
                }:
                    raise ReadFailure("MEDIA_CONTAINER_UNSUPPORTED")
                length = info.get("content-length")
                if length is not None and (not re.fullmatch(r"[1-9][0-9]{0,10}", length)
                                           or int(length) > maximum):
                    raise ReadFailure("READER_BYTE_LIMIT")
                body, size, digest = bytearray(), 0, hashlib.sha256()
                async for chunk in response.aiter_stream():
                    self.budget.used_bytes += len(chunk)
                    size += len(chunk)
                    if size > maximum or self.budget.used_bytes > self.budget.max_bytes:
                        raise ReadFailure("READER_BYTE_LIMIT")
                    await self.guard()
                    self.account()
                    digest.update(chunk)
                    if sink is None:
                        body.extend(chunk)
                    else:
                        sink.write(chunk)
                if not size or length is not None and size != int(length):
                    raise ReadFailure("MEDIA_BODY_INCOMPLETE")
            await self.guard()
            self.account()
            return bytes(body), size, digest.hexdigest()
        except ReadFailure as error:
            self.denied(error)
            raise

    async def api(self, path, query=""):
        if path not in API_PATHS:
            raise ReadFailure("CONTENT_URL_NOT_ALLOWED")
        raw, _, digest = await self.transfer(
            API_ORIGIN + path + ("?" + query if query else ""),
            maximum=MAX_METADATA_BYTES - self.budget.used_bytes,
        )
        try:
            data = api_data(raw)
        except ReadFailure as error:
            self.denied(error)
            raise
        return data, digest

    async def resolve(self, media, limits, now):
        view, view_sha = await self.api("/x/web-interface/view", urlencode({"bvid": media["bvid"]}))
        pages = view.get("pages")
        if (view.get("bvid") != media["bvid"] or view.get("cid") != media["cid"]
            or not isinstance(pages, list) or not pages or not isinstance(pages[0], dict)
            or pages[0].get("page") != 1 or pages[0].get("cid") != media["cid"]
            or pages[0].get("duration") != media["metadata_duration_seconds"]):
            raise ReadFailure("READER_TARGET_MISMATCH")
        nav, nav_sha = await self.api("/x/web-interface/nav")
        play, play_sha = await self.api("/x/player/wbi/playurl", signed_query({
            "bvid": media["bvid"], "cid": media["cid"], "fnval": 4048,
        }, nav, now))
        milliseconds = play.get("timelength")
        if (type(milliseconds) is not int
            or not 1 <= milliseconds <= limits.max_source_seconds * 1000
            or abs(milliseconds / 1000 - media["metadata_duration_seconds"]) > 1
            or any(key in play and play[key] != media[key] for key in ("bvid", "cid"))):
            raise ReadFailure("READER_TARGET_MISMATCH")
        row, locator = representation(play, limits)
        identity = {"bvid": media["bvid"], "cid": media["cid"], "part": 1,
                    "duration_seconds": milliseconds / 1000}
        proof = {
            "resolver": "bilibili_wbi_playurl_v1", "view_response_sha256": view_sha,
            "nav_response_sha256": nav_sha, "playinfo_response_sha256": play_sha,
            "representation_sha256": json_sha256(row),
            "locator_sha256": hashlib.sha256(locator.encode()).hexdigest(),
            "mime_type": row.get("mimeType", row.get("mime_type")), "codec": row["codecs"],
            "width": row["width"], "height": row["height"], "declared_bytes": row.get("size"),
            "duration_milliseconds": milliseconds,
        }
        return identity, proof, locator


async def post_once(core, path, payload, phase, binding, *, deadline, clock):
    """Query only the original digest after a transport/5xx ambiguity."""
    remaining = (deadline - clock()).total_seconds()
    if remaining <= 0:
        raise ReadingStopped("STALE_READER_LEASE")
    try:
        response = await core.post(path, json=payload, timeout=remaining)
        if response.status_code < 500:
            if response.status_code != 200:
                raise ReadFailure("MEDIA_CORE_REJECTED")
            return response.json()
    except (httpx.HTTPError, TimeoutError):
        pass
    receipt = {**binding, "phase": phase, "request_sha256": json_sha256(payload)}
    receipt_path = path.removesuffix("/prepare")
    while (remaining := (deadline - clock()).total_seconds()) > 0:
        try:
            response = await core.post(receipt_path + "/receipt", json=receipt,
                                       timeout=min(2, remaining))
            response.raise_for_status()
            saved = response.json()
            if isinstance(saved, dict) and saved.get("state") == "FOUND":
                exact(saved, {"state", "response"})
                return saved["response"]
            exact(saved, {"state"})
            if saved["state"] == "NOT_FOUND":
                break
            if saved["state"] != "PENDING":
                raise ValueError("invalid receipt state")
        except (httpx.HTTPError, TimeoutError):
            break
        await asyncio.sleep(min(0.5, remaining))
    raise ReadingStopped("MEDIA_RECEIPT_UNKNOWN")


async def run_media(core, reader, item, context, path, validate_read, *, limits, until,
                    cancel_event, clock=lambda: datetime.now(UTC), work_dir=None):
    budget = ReadBudget(item["max_bytes"], item["max_requests"])
    binding = {"schema_version": 1, "lease_token": item["lease_token"],
               "lease_sha256": json_sha256(item)}
    if item.get("source_grant") is not None:
        binding["source_grant"] = item["source_grant"]
    result = {"status": "SUCCESS", "prepare_sha256": None,
              "phase_receipt_sha256": [], "error_code": None}

    async def guard():
        if cancel_event.is_set():
            raise ReadingStopped("READ_CANCELLED")
        if clock() >= until:
            raise ReadingStopped("STALE_READER_LEASE")
        try:
            await validate_read(context)
            if getattr(reader, "grant_snapshot", lambda **kw: None)(required=True) != item.get(
                "source_grant",
            ):
                raise ReadFailure("SOURCE_GRANT_REVISION_CHANGED")
        except ReadFailure as error:
            raise ReadingStopped(str(error)) from error

    budget.validate_account_read = guard
    try:
        root = Path(work_dir or os.environ.get("TMPDIR")
                    or PACKAGE_ROOT / ".local" / "media-temp")
        root = await asyncio.to_thread(root.resolve)
        await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
        async with asyncio.timeout(max(0.001, (until - clock()).total_seconds())):
            transport = MediaHTTP(reader.public, budget, guard)
            identity, proof, locator = await transport.resolve(item["media"], limits, clock())
            request = {**binding, "identity": identity, "proof": proof}
            await guard()
            reply = await post_once(core, path + "/media/prepare", request, "prepare", binding,
                                    deadline=until, clock=clock)
            target, selected, limits = prepare_response(reply, request, item["media"])
            result["prepare_sha256"] = reply["prepare_sha256"]
            await guard()
            with tempfile.TemporaryDirectory(prefix="bili-media-", dir=root) as temporary:
                local = Path(temporary) / "source.bin"
                with local.open("xb") as sink:
                    _, size, digest = await transport.transfer(
                        locator, maximum=limits.max_input_bytes, sink=sink, mime=proof["mime_type"],
                    )
                if proof["declared_bytes"] is not None and size != proof["declared_bytes"]:
                    raise ReadFailure("MEDIA_BODY_INCOMPLETE")
                downloaded = DownloadedMedia(
                    target.sha256, str(local), digest, size, proof["mime_type"],
                    ClipRange(0, target.duration_seconds),
                )
                output_bytes = 0
                for _ in range(2):
                    assets = await extract_media(
                        target, selected, limits, downloaded, validate=guard,
                        work_dir=Path(temporary) / "extract", cancel_event=cancel_event,
                    )
                    output_bytes += sum(asset.manifest.byte_length for asset in assets)
                    if output_bytes > limits.max_total_output_bytes:
                        raise ReadFailure("MEDIA_OUTPUT_LIMIT")
                    request = {
                        **binding, "prepare_sha256": result["prepare_sha256"],
                        "stage": selected.kind, "selection_sha256": selected.sha256,
                        "download": {"input_sha256": digest, "input_bytes": size,
                                     "mime_type": proof["mime_type"],
                                     "source_range": asdict(downloaded.source_range)},
                        "assets": assets_to_wire(assets),
                    }
                    await guard()
                    reply = await post_once(core, path + "/media", request, selected.kind,
                                            binding, deadline=until, clock=clock)
                    following = upload_response(reply, request, target, selected, limits)
                    result["phase_receipt_sha256"].append(reply["receipt_sha256"])
                    if following is None:
                        break
                    selected = following
                await guard()
    except ReadingStopped as error:
        result.update(status="UNKNOWN" if str(error) == "MEDIA_RECEIPT_UNKNOWN" else "CANCELLED",
                      error_code=str(error))
    except (ReadFailure, MediaExtractionError) as error:
        result.update(status="FAILED", error_code=str(error))
    except (TimeoutError, OSError, httpcore.NetworkError, httpcore.TimeoutException,
            httpcore.ProtocolError, httpx.HTTPError):
        result.update(status="UNKNOWN", error_code="READER_NETWORK_UNKNOWN")
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError, OverflowError):
        result.update(status="FAILED", error_code="MEDIA_RESPONSE_INVALID")
    uncertain = result["status"] == "UNKNOWN" or (
        result["status"] == "CANCELLED" and budget.used_requests > 0
    )
    result.update(
        used_requests=None if uncertain else budget.used_requests,
        used_bytes=None if uncertain or budget.used_bytes > budget.max_bytes else budget.used_bytes,
        observed_requests=budget.used_requests, observed_bytes=budget.used_bytes,
    )
    response = await core.post(path + "/media/result", json={**binding, **result})
    response.raise_for_status()
    return True

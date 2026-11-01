"""One bounded public Bilibili song audio transfer with the existing pinned transport.

The supervising caller supplies live authorization and owns cleanup. No account file,
Cookie, login fallback, retries, or model-selected locator is accepted. Select one
allowed API-declared locator before I/O; never switch after an access denial.
"""

import asyncio
import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from companion.bili_video_protocol import MAX_METADATA_BYTES, json_sha256
from source_reader.bili_media import API_ORIGIN, MediaHTTP, media_locator
from source_reader.bili_signing import signed_query
from source_reader.network import ReadBudget, ReadFailure

MAX_SONG_BYTES = 32 * 1024 * 1024
MAX_SONG_SECONDS = 600


@dataclass(frozen=True)
class DownloadedSong:
    path: Path
    identity: dict
    proof: dict
    sha256: str
    byte_length: int


def representation(data, *, max_seconds=MAX_SONG_SECONDS, max_bytes=MAX_SONG_BYTES):
    if not isinstance(data, dict) or not isinstance(data.get("dash"), dict):
        raise ReadFailure("SINGING_AUDIO_UNAVAILABLE")
    rows = data["dash"].get("audio")
    duration = data.get("timelength")
    if (not isinstance(rows, list) or not 1 <= len(rows) <= 32 or type(duration) is not int
        or not 0 < duration <= max_seconds * 1000):
        raise ReadFailure("SINGING_AUDIO_UNAVAILABLE")
    candidates = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        mime = row.get("mimeType", row.get("mime_type"))
        bandwidth = row.get("bandwidth")
        if (mime != "audio/mp4" or row.get("codecs") != "mp4a.40.2"
            or type(bandwidth) is not int or not 1 <= bandwidth <= 400000
            or math.ceil(bandwidth * duration / 8000) > max_bytes):
            continue
        backups = row.get("backupUrl", row.get("backup_url", []))
        if not isinstance(backups, list) or len(backups) > 8:
            continue
        locators = [row.get("baseUrl", row.get("base_url")), *backups]
        for locator in locators:
            try:
                media_locator(locator, audio=True)
            except (ReadFailure, ValueError):
                continue
            candidates.append((bandwidth, -index, row, locator))
            break
    if not candidates:
        raise ReadFailure("SINGING_AUDIO_UNAVAILABLE")
    _, _, row, locator = max(candidates, key=lambda item: item[:2])
    return row, locator


async def download_song(public, bvid, part, destination, *, guard, now,
                        max_seconds=MAX_SONG_SECONDS, max_bytes=MAX_SONG_BYTES):
    """Fetch one exact part; the guard must recheck the original live Core lease."""
    if (not isinstance(bvid, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid)
        or type(part) is not int or not 1 <= part <= 100
        or type(max_seconds) is not int or not 1 <= max_seconds <= MAX_SONG_SECONDS
        or type(max_bytes) is not int or not 1024 <= max_bytes <= MAX_SONG_BYTES):
        raise ReadFailure("SINGING_TARGET_INVALID")
    destination = Path(destination)
    if (not destination.is_absolute() or destination.resolve() != destination.absolute()
        or str(destination).startswith(("\\\\", "//")) or destination.exists()):
        raise ReadFailure("SINGING_DESTINATION_INVALID")
    if getattr(public, "verify", None) is not None:
        raise ReadFailure("SINGING_PUBLIC_TRANSPORT_REQUIRED")
    budget = ReadBudget(max_bytes + MAX_METADATA_BYTES, 4)
    budget.validate_account_read = guard
    transfer = MediaHTTP(public, budget, guard, audio=True)
    view, view_sha = await transfer.api("/x/web-interface/view", urlencode({"bvid": bvid}))
    pages = view.get("pages")
    if (view.get("bvid") != bvid or not isinstance(pages, list)
        or not part <= len(pages) <= 1000 or not isinstance(pages[part - 1], dict)):
        raise ReadFailure("READER_TARGET_MISMATCH")
    page = pages[part - 1]
    if (page.get("page") != part or type(page.get("cid")) is not int or page["cid"] <= 0
        or type(page.get("duration")) is not int or not 0 < page["duration"] <= max_seconds):
        raise ReadFailure("SINGING_TARGET_INVALID")
    # Anonymous nav convention is -101/isLogin=false with public WBI image keys.
    # This exception applies only to nav; view/playurl still require API code 0.
    raw, _, nav_sha = await transfer.transfer(API_ORIGIN + "/x/web-interface/nav",
                                              maximum=MAX_METADATA_BYTES - budget.used_bytes)
    try:
        nav = json.loads(raw)
        data = nav["data"]
        if nav.get("code") not in {0, -101} or (
            nav.get("code") == -101 and data.get("isLogin") is not False
        ):
            raise ValueError
        query = signed_query({"bvid": bvid, "cid": page["cid"], "fnval": 4048}, data, now)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ReadFailure("UPSTREAM_RESPONSE_INVALID") from None
    play, play_sha = await transfer.api("/x/player/wbi/playurl", query)
    milliseconds = play.get("timelength")
    if (type(milliseconds) is not int or abs(milliseconds / 1000 - page["duration"]) > 1
        or any(key in play and play[key] != value for key, value in (
            ("bvid", bvid), ("cid", page["cid"])) )):
        raise ReadFailure("READER_TARGET_MISMATCH")
    row, locator = representation(play, max_seconds=max_seconds, max_bytes=max_bytes)
    with await asyncio.to_thread(destination.open, "xb") as output:
        _, size, digest = await transfer.transfer(locator, maximum=max_bytes,
                                                   sink=output, mime="audio/mp4")
    await guard()
    return DownloadedSong(destination,
        {"bvid": bvid, "cid": page["cid"], "part": part,
         "duration_seconds": milliseconds / 1000},
        {"resolver": "bilibili_public_song_audio_v1", "view_response_sha256": view_sha,
         "nav_response_sha256": nav_sha, "playinfo_response_sha256": play_sha,
         "representation_sha256": json_sha256(row),
         "locator_sha256": hashlib.sha256(locator.encode()).hexdigest(),
         "mime_type": "audio/mp4", "codec": "mp4a.40.2", "audio_sha256": digest,
         "audio_bytes": size, "used_requests": budget.used_requests,
         "used_bytes": budget.used_bytes}, digest, size)

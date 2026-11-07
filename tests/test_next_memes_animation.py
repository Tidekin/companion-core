import asyncio
import hashlib
import io
import json

import httpx
import pytest
from PIL import Image

from companion_meme_sources import ReviewApproval
from companion_onebot_bridge.meme_discovery import BraveImages, run_discovery
from tests.support.next_memes_animation import canvas_gif
from tests.support.next_memes_catalog import check, metadata
from tests.support.next_memes_discovery import async_check, resolver
from tests.support.next_memes_discovery import setup as setup


def decoded_sizes(data):
    with Image.open(io.BytesIO(data)) as image:
        sizes = []
        for index in range(image.n_frames):
            image.seek(index)
            image.load()
            sizes.append(image.size)
        return sizes


@pytest.mark.parametrize("sizes,pixel_limit,expected", [
    ([(24, 24), (2049, 24)], 16000000, None),
    ([(24, 24), (1000, 1000), (1000, 1000)], 2000575, None),
    ([(24, 24), (24, 24)], 1152, (24, 24)),
    ([(24, 24), (48, 32)], 2112, (48, 32)),
])
def test_network_animation_admission_and_preparation_keep_actual_limits_and_usage(
    setup, sizes, pixel_limit, expected,
):
    catalog, budget = setup
    catalog.library.max_total_pixels = pixel_limit
    body = canvas_gif(sizes)
    assert decoded_sizes(body) == sizes
    if sizes[-1] == (2049, 24):
        assert len(body) == 1213
    calls, downloaded, reviews = [], [], []

    def handle(request):
        calls.append(request)
        if request.headers["Host"] == "api.search.brave.com":
            response = httpx.Response(200, json={"results": [{
                "url": "https://licensed.example/page",
                "properties": {"url": "https://licensed.example/image.gif"},
            }]})
        else:
            response = httpx.Response(200, content=body, headers={"Content-Type": "image/gif"})
        downloaded.append(len(response.content))
        return response

    async def review(candidate, image, evidence):
        reviews.append(image.sha256)
        return ReviewApproval(metadata(
            source_page=candidate.source_page, asset_url=candidate.asset_url,
            fetched_at=evidence.fetch.fetched_at,
        ), evidence.sha256)

    def attempt():
        return asyncio.run(run_discovery(
            "gif-dimension", "celebrate", BraveImages("synthetic-token"), catalog,
            budget=budget, check=async_check, review=review, commit_check=check,
            transport=httpx.MockTransport(handle), resolver=resolver,
        ))

    result = attempt()
    offers = catalog.candidates("delivery", "celebrate", check=check)
    if expected is None:
        assert result["accepted"] == [] and result["rejected"] == 1
        assert offers == [] and reviews == []
        assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 0
    else:
        assert result["accepted"] == reviews == [hashlib.sha256(body).hexdigest()]
        assert result["rejected"] == 0 and len(offers) == 1
        image = catalog.prepare_selection("delivery", offers[0]["candidate_id"], check=check)
        assert (image.width, image.height) == expected and image.data == body
    assert result["usage"] == {
        "queries": 1, "requests": 2, "download_bytes": sum(downloaded),
        "storage_bytes": 0 if expected is None else len(body), "model_cost": 0,
    }
    with pytest.raises(ValueError, match="ALREADY_RESERVED"):
        attempt()
    assert len(calls) == 2 and len(reviews) == (0 if expected is None else 1)


@pytest.mark.parametrize("sizes,limit,expected", [
    ([(24, 24), (24, 24)], 1152, (24, 24)),
    ([(24, 24), (48, 32)], 2112, (48, 32)),
    ([(24, 24), (1000, 1000), (1000, 1000)], 2000575, None),
    ([(24, 24), (1000, 1000), (1000, 1000)], 2000576, (1000, 1000)),
])
def test_animation_counts_actual_canvases_and_returns_maximum_dimensions(setup, sizes, limit,
                                                                       expected):
    catalog, _ = setup
    body = canvas_gif(sizes)
    assert decoded_sizes(body) == sizes
    catalog.library.max_total_pixels = limit
    image = catalog.library.validate_bytes(body)
    if expected is None:
        assert image is None
    else:
        assert (image.width, image.height) == expected
        assert image.data == body and image.sha256 == hashlib.sha256(body).hexdigest()


def test_frame_offset_expands_canvas_and_normal_animation_keeps_frame_cap(setup):
    catalog, _ = setup
    body = canvas_gif([(24, 24), (24, 24)], offsets=[(0, 0), (2025, 0)])
    assert decoded_sizes(body) == [(24, 24), (2049, 24)]
    assert catalog.library.validate_bytes(body) is None
    catalog.library.max_frames = 1
    assert catalog.library.validate_bytes(canvas_gif([(24, 24), (24, 24)])) is None


def test_cancelled_animation_download_cannot_repeat_request_or_budget(setup):
    catalog, budget = setup
    body = canvas_gif([(24, 24), (24, 24)])
    stopped, calls, downloaded = False, [], []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal stopped
            stopped = True
            downloaded.append(len(body))
            yield body

    def handle(request):
        calls.append(request)
        if request.headers["Host"] == "api.search.brave.com":
            response = httpx.Response(200, json={"results": [{
                "url": "https://licensed.example/page",
                "properties": {"url": "https://licensed.example/image.gif"},
            }]})
            downloaded.append(len(response.content))
            return response
        return httpx.Response(200, stream=Chunks(), headers={"Content-Type": "image/gif"})

    async def fence():
        if stopped:
            raise ValueError("RECALLED")

    async def no_review(*args):
        raise AssertionError("cancelled animation reached review")

    def attempt(guard):
        return asyncio.run(run_discovery(
            "cancelled-gif", "celebrate", BraveImages("synthetic-token"), catalog,
            budget=budget, check=guard, review=no_review, commit_check=check,
            transport=httpx.MockTransport(handle), resolver=resolver,
        ))

    with pytest.raises(ValueError, match="RECALLED"):
        attempt(fence)
    state, actual = catalog.db.execute("SELECT state,actual FROM test_meme_budget").fetchone()
    assert state == "UNKNOWN"
    assert json.loads(actual) == {
        "queries": 1, "requests": 2, "download_bytes": sum(downloaded),
        "storage_bytes": 0, "model_cost": 0,
    }
    with pytest.raises(ValueError, match="ALREADY_RESERVED"):
        attempt(async_check)
    assert len(calls) == 2
    assert catalog.db.execute("SELECT COUNT(*) FROM meme_assets").fetchone()[0] == 0

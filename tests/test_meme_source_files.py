"""Public source-file witnesses survive native caption admission and accounting."""

import base64
import hashlib
import json
from datetime import timedelta

import pytest

from companion_meme_protocol import CaptionOutcome, digest
from tests.conftest import run
from tests.support.meme_core import body, caption_environment, worker

REVISION = "a" * 40
RAW_ROOT = f"https://raw.githubusercontent.com/owner/repo/{REVISION}"
LICENSE = b"Synthetic CC0 license for this test fixture.\n"
SOURCE_ROLES = ("manifest", "license", "listing")


def fetch(url, data, fetched_at):
    return {
        "requested_url": url,
        "final_url": url,
        "hops": [{"url": url, "address": "8.8.8.8", "status": 200}],
        "body_sha256": hashlib.sha256(data).hexdigest(),
        "fetched_at": fetched_at.isoformat(),
    }


def source_files_body(core, event, item, now):
    payload = body(core, event, item, now)
    asset_url = RAW_ROOT + "/memes/happy/image.png"
    page = f"https://github.com/owner/repo/tree/{REVISION}"
    manifest = json.dumps({"schema_version": 1, "categories": ["happy"]}).encode()
    listing = json.dumps([
        {"type": "file", "path": "memes/happy/image.png", "download_url": asset_url}
    ]).encode()
    source = payload["source_evidence"]
    source.update(
        source_page=page,
        asset_url=asset_url,
        revision=REVISION,
        usage_basis="Synthetic public-domain fixture; license-file=LICENSE.md; license-sha256="
        + hashlib.sha256(LICENSE).hexdigest(),
    )
    source["acquisition"].update(
        source_page=page,
        discovered_asset_url=asset_url,
        revision=REVISION,
        fetch=fetch(asset_url, base64.b64decode(payload["asset"]["data_base64"]), now),
        source_files={
            "manifest": fetch(RAW_ROOT + "/manifest.json", manifest, now - timedelta(seconds=3)),
            "license": fetch(RAW_ROOT + "/LICENSE.md", LICENSE, now - timedelta(seconds=2)),
            "listing": fetch(
                "https://api.github.com/repos/owner/repo/contents/memes/happy?ref=" + REVISION,
                listing,
                now - timedelta(seconds=1),
            ),
        },
    )
    payload["asset"]["source_evidence_sha256"] = digest(source)
    return payload


@pytest.mark.parametrize("source_files", [True, False], ids=["source-files", "legacy"])
def test_caption_source_witness_persists_and_replay_never_rebills(
    core, tmp_path, monkeypatch, source_files
):
    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, _, client, _, _, now, item = values
            payload = (source_files_body if source_files else body)(core, event, item, now)
            first = await client.post("/internal/v1/memes/captions", json=payload)
            assert first.status_code == 200, first.text
            queued = CaptionOutcome.model_validate_json(first.text)
            assert queued.state == "QUEUED" and queued.usage is None
            repeated = await client.post("/internal/v1/memes/captions", json=payload)
            assert repeated.status_code == 200 and repeated.json() == first.json()

            calls = []
            assert await worker(core, calls)
            response = await client.post("/internal/v1/memes/captions/" + queued.job_id, json={})
            assert response.status_code == 200, response.text
            completed = CaptionOutcome.model_validate_json(response.text)
            assert completed.state == "SUCCEEDED"
            assert completed.usage.state == "SETTLED" and completed.usage.billed_micro_usd > 0
            stored = core.store.one('SELECT * FROM meme_captions WHERE job_id=%s', (queued.job_id,))
            assert json.loads(stored["source_json"]) == payload["source_evidence"]
            assert stored["source_sha256"] == payload["asset"]["source_evidence_sha256"]
            assert stored["asset_data"] == base64.b64decode(payload["asset"]["data_base64"])
            assert stored["usage_request_id"] == completed.usage.usage_request_id

            replay = await client.post("/internal/v1/memes/captions", json=payload)
            assert replay.status_code == 200 and replay.json() == response.json()
            assert not await worker(core, calls)
            assert len(calls) == 1
            assert len(core.store.rows("SELECT * FROM jobs WHERE kind='meme_caption'")) == 1
            assert len(core.store.rows("SELECT * FROM model_usage")) == 1
            assert not core.store.rows("SELECT * FROM outbox")

    run(scenario())


def change_url(receipt, url):
    receipt.update(requested_url=url, final_url=url)
    receipt["hops"][0]["url"] = url


INVALID_PROOFS = [
    "license-hash",
    "declared-license-hash",
    "license-path",
    "listing-parent-directory",
    "listing-other-category",
    "listing-missing-ref",
    "listing-extra-query",
    "missing-source-key",
    "extra-source-key",
    "empty-source-files",
    "null-source-files",
    *[f"{role}:{defect}" for role in SOURCE_ROLES for defect in (
        "repo", "revision", "private-ip", "redirect", "final-url", "missing-fetch-key",
        "extra-fetch-key", "naive-time", "non-200",
    )],
]


def invalidate(source, case):
    files = source["acquisition"]["source_files"]
    if ":" in case:
        role, defect = case.split(":")
        receipt = files[role]
        url = receipt["requested_url"]
        if defect == "repo":
            change_url(receipt, url.replace("owner/repo/", "owner/other/"))
        elif defect == "revision":
            change_url(receipt, url.replace(REVISION, "b" * 40))
        elif defect == "private-ip":
            receipt["hops"][0]["address"] = "127.0.0.1"
        elif defect == "redirect":
            receipt["hops"] = [
                {"url": url, "address": "8.8.8.8", "status": 302},
                {"url": url, "address": "8.8.4.4", "status": 200},
            ]
        elif defect == "final-url":
            receipt["final_url"] = "https://public.example/unrelated"
            receipt["hops"][0]["url"] = receipt["final_url"]
        elif defect == "missing-fetch-key":
            receipt.pop("body_sha256")
        elif defect == "extra-fetch-key":
            receipt["approved"] = True
        elif defect == "naive-time":
            receipt["fetched_at"] = "2026-10-19T04:00:00"
        elif defect == "non-200":
            receipt["hops"][0]["status"] = 204
    elif case == "license-hash":
        files["license"]["body_sha256"] = "0" * 64
    elif case == "declared-license-hash":
        source["usage_basis"] = source["usage_basis"].rsplit("=", 1)[0] + "=" + "1" * 64
    elif case == "license-path":
        change_url(files["license"], RAW_ROOT + "/OTHER-LICENSE.md")
    elif case.startswith("listing-"):
        url = files["listing"]["requested_url"]
        replacements = {
            "listing-parent-directory": url.replace("/contents/memes/happy?", "/contents/memes?"),
            "listing-other-category": url.replace("/memes/happy?", "/memes/sad?"),
            "listing-missing-ref": url.split("?", 1)[0],
            "listing-extra-query": url + "&untrusted=1",
        }
        change_url(files["listing"], replacements[case])
    elif case == "missing-source-key":
        files.pop("license")
    elif case == "extra-source-key":
        files["other"] = files["manifest"]
    elif case == "empty-source-files":
        files.clear()
    elif case == "null-source-files":
        source["acquisition"]["source_files"] = None


@pytest.mark.parametrize("case", INVALID_PROOFS)
def test_invalid_source_files_are_rejected_before_caption_or_model_admission(
    core, tmp_path, monkeypatch, case
):
    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, _, client, _, _, now, item = values
            payload = source_files_body(core, event, item, now)
            invalidate(payload["source_evidence"], case)
            # A valid outer digest makes each case exercise its actual inner proof boundary.
            payload["asset"]["source_evidence_sha256"] = digest(payload["source_evidence"])
            rejected = await client.post("/internal/v1/memes/captions", json=payload)
            assert rejected.status_code == 403, rejected.text
            assert rejected.json()["error"] == "MEME_SOURCE_EVIDENCE_INVALID"
            assert not core.store.rows("SELECT * FROM meme_captions")
            assert not core.store.rows("SELECT * FROM jobs WHERE kind='meme_caption'")
            assert not core.store.rows("SELECT * FROM model_usage")
            assert not core.store.rows("SELECT * FROM outbox")

    run(scenario())

import base64
import io
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr

from companion.api import create_app
from companion.backup import backup, restore
from companion.content import ContentService, validate_image
from companion.legacy_import import migrate_sqlite
from companion.media_provider import HTTPMediaProvider
from companion.postgres import Database
from companion.store import Store
from source_reader.heybox import render_article
from tests.conftest import run
from tests.postgres_fixture import isolated_database
from tests.support.article_vision import article
from tests.support.content import CDN, enable, png
from tests.support.release_migration import insert, one, open_historical, rows, seed_legacy


def image_transport():
    return httpx.MockTransport(
        lambda request: httpx.Response(200, content=png(), headers={"content-type": "image/png"})
    )


def test_eight_images_reuse_chat_provider_in_two_image_batches_and_keep_article(core, tmp_path):
    service, _ = enable(core, use_chat_model_for_vision=True)
    core.settings.model_url = "https://api.deepseek.com"
    core.settings.model_name = "deepseek-flash"
    core.settings.model_key = SecretStr("fixture-deepseek-key")
    service = ContentService(
        core.store,
        core.settings,
        core.observations,
        core.exploration.create,
        core.clock,
        options=service.options,
        image_transport=image_transport(),
    )
    core.content = service
    core.exploration.register_link_images = service.register_link_images
    calls = []

    def provider(request):
        payload = json.loads(request.content)
        assert request.url == "https://api.deepseek.com/chat/completions"
        assert request.headers["authorization"] == "Bearer fixture-deepseek-key"
        assert payload["model"] == "deepseek-flash"
        assert payload["thinking"] == {"type": "disabled"}
        data_url = payload["messages"][0]["content"][1]["image_url"]["url"]
        assert base64.b64decode(data_url.split(",", 1)[1]) == png()
        calls.append(payload)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": f"识别第{len(calls)}张图：图表上的数值是42。"},
                        "finish_reason": "length" if len(calls) == 1 else "stop",
                    }
                ]
            },
        )

    service.provider = HTTPMediaProvider(service.options, httpx.MockTransport(provider))
    owner, source = article(core)
    for count in (2, 4, 6, 8):
        assert run(service.run_batch()) == 2
        assert len(calls) == count
    assert not run(service.run_batch())
    rows = json.loads(service.context_message(owner, 12000))["shared_content"]
    assert "文章的真实正文" in rows[0]["text"]
    assert rows[0]["image_progress"] == {
        "total": 8,
        "recognized": 8,
        "queued": 0,
        "failed": 0,
        "unknown": 0,
    }
    assert rows[0]["image_details_omitted"] == 0
    assert [row["image_index"] for row in rows[1:]] == list(range(1, 9))
    assert all(row["article_event_id"] == source and row["text_offset"] > 0 for row in rows[1:])
    assert "识别第8张图" in rows[-1]["text"]
    assert "仅为部分内容" in rows[1]["text"]
    assert all(row["trust"] == "external_untrusted" for row in rows)
    compact = service.context_message(owner, 1200)
    assert len(compact.encode()) <= 1200
    assert json.loads(compact)["shared_content"][0]["image_details_omitted"] > 0
    assert "fixture-deepseek-key" not in compact

    key, archive = Fernet.generate_key(), tmp_path / "before.enc"
    backup(core.settings.database, archive, key)
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, [source], core.clock())
    with isolated_database() as destination:
        state = tmp_path / "restored-state"
        restore(archive, destination, key, core.settings.erasure_ledger, state_directory=state)
        with Database(destination) as database, closing(Store(database, state)) as restored:
            assert all(
                row["redacted"] and not row["target_url"]
                for row in restored.rows("SELECT * FROM content_items WHERE kind='link_image'")
            )
            assert all(
                row["redacted"]
                for row in restored.rows(
                    "SELECT * FROM observations WHERE source_kind='image_analysis'"
                )
            )
            assert not restored.rows(
                "SELECT 1 FROM pg_constraint WHERE "
                "connamespace=current_schema()::regnamespace AND NOT convalidated"
            )


def test_article_money_cap_is_advisory_and_completed_images_never_repeat(core):
    service, vision = enable(core, article_image_micro_usd=20)
    gets = []

    def download(request):
        gets.append(str(request.url))
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

    service.image_transport = httpx.MockTransport(download)
    owner, _ = article(core, 5)
    while run(service.run_batch()):
        pass
    assert len(gets) == len(vision.calls) == 5
    assert not core.store.rows("SELECT * FROM content_items WHERE state='FAILED'")
    core.clock.advance(31)
    service.options.article_image_micro_usd = 50
    assert service.resume_link_images(owner)["resumed"] == 0
    assert not service.status(owner)["pending"]
    while run(service.run_batch()):
        pass
    assert len(gets) == len(set(gets)) == len(vision.calls) == 5
    assert service.resume_link_images(owner)["resumed"] == 0
    assert core.store.one("SELECT SUM(billed_micro_usd) AS n FROM model_usage")["n"] == 50


def test_resume_requires_authenticated_owner_and_does_not_restart_unknown_images(core):
    service, vision = enable(core)
    service.image_transport = image_transport()
    owner, _ = article(core, 1)
    vision.error = httpx.ReadTimeout("uncertain")
    assert run(service.run_once())
    assert service.resume_link_images(owner)["resumed"] == 0
    assert not run(service.run_once())
    assert len(vision.calls) == 1
    core.settings.content = service.options
    with TestClient(create_app(core.settings)) as client:
        path = f"/internal/v2/content/{owner}/images/resume"
        assert client.post(path).status_code == 401
        auth = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
        assert client.post(path, headers=auth).json()["resumed"] == 0
        assert (
            client.post(path.replace(owner, "not-an-owner-event"), headers=auth).status_code == 403
        )


def test_article_limit_retains_unread_references_and_can_resume_after_limit_increase(core):
    service, vision = enable(core, max_images_per_article=2)
    service.image_transport = image_transport()
    owner, _ = article(core, 5)
    assert len(core.store.rows("SELECT * FROM content_items WHERE kind='link_image'")) == 5
    assert run(service.run_batch()) == 2
    assert len(vision.calls) == 2
    service.options.max_images_per_article = 5
    assert service.resume_link_images(owner)["resumed"] == 3
    assert run(service.run_batch()) == 2
    assert run(service.run_batch()) == 1
    assert len(vision.calls) == 5


def test_reader_retains_32_ordered_images_and_marks_larger_articles_truncated():
    _, images, truncated = render_article(
        [
            ("text", "图片之前的正文"),
            *[("image", CDN + str(i)) for i in range(35)],
        ]
    )
    assert len(images) == 32 and truncated
    assert images[-1]["url"] == CDN + "31"
    assert [image["text_offset"] for image in images] == sorted(i["text_offset"] for i in images)


def test_long_prose_does_not_discard_later_article_images():
    text, images, truncated = render_article(
        [
            ("text", "文章正文" * 5000),
            *[("image", CDN + str(i)) for i in range(8)],
        ]
    )
    assert len(text) <= 11500 and len(images) == 8 and truncated
    assert all(text[image["text_offset"] :].startswith("[图片") for image in images)


def test_static_webp_is_accepted_and_animated_webp_is_rejected():
    output = io.BytesIO()
    first, second = Image.new("RGB", (16, 16), "red"), Image.new("RGB", (16, 16), "blue")
    first.save(output, format="WEBP")
    validate_image(output.getvalue(), "image/webp", 100000)
    output = io.BytesIO()
    first.save(output, format="WEBP", save_all=True, append_images=[second], duration=100, loop=0)
    with pytest.raises(Exception, match="CONTENT_IMAGE_INVALID"):
        validate_image(output.getvalue(), "image/webp", 100000)


def test_populated_0014_upgrade_keeps_rows_constraints_and_refuses_lossy_downgrade(tmp_path):
    database, _, _ = seed_legacy(tmp_path, "0004_diary")
    migrate_sqlite(str(database), "0014_reader_budget")
    with closing(open_historical(str(database))) as store:
        insert(
            store,
            "content_items",
            item_id="old-content",
            owner_event_id="evt_keep",
            kind="link_image",
            slot=3,
            source_event_id="evt_keep",
            target_url=CDN,
            state="UNKNOWN",
            captured_privacy_epoch=3,
            reserved_bytes=123,
            deadline="2026-10-18T04:00:00+00:00",
            created_at="2026-10-18T04:00:00+00:00",
            updated_at="2026-10-18T04:00:00+00:00",
        )
        before = rows(store, "SELECT * FROM content_items ORDER BY item_id")
    migrate_sqlite(str(database), "0015_article_images")
    with closing(open_historical(str(database))) as store:
        assert rows(store, "SELECT * FROM content_items ORDER BY item_id") == before
        row = {**before[0], "item_id": "fifth-image", "slot": 4}
        insert(store, "content_items", **row)
        for changes in ({"state": "INVALID"}, {"kind": "INVALID"}, {"slot": 64}):
            with pytest.raises(sqlite3.IntegrityError):
                insert(store, "content_items", **{**row, "item_id": "bad", **changes})
        assert not rows(store, "PRAGMA foreign_key_check")
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    config.set_main_option("sqlalchemy.url", "sqlite:///" + database.as_posix())
    with pytest.raises(RuntimeError, match="ARTICLE_IMAGES_DOWNGRADE"):
        command.downgrade(config, "0014_reader_budget")
    with closing(open_historical(str(database))) as store:
        assert one(store, "SELECT version_num FROM alembic_version")["version_num"] == (
            "0015_article_images"
        )
        assert rows(store, "SELECT * FROM content_items ORDER BY item_id") == sorted(
            [*before, row],
            key=lambda item: item["item_id"],
        )
        assert not rows(store, "PRAGMA foreign_key_check")

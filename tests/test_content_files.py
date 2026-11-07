import asyncio
import hashlib
import json
import shutil
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from alembic import command
from cryptography.fernet import Fernet

from companion import content_files, file_parser, image_spool, qq_files
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from companion.file_evidence import PrivateFilesConfig
from companion.privacy import advance_epoch
from companion.store import Store, migrate
from tests.conftest import run
from tests.support.input_reply_migration import config_for
from tests.test_qq_files import IMAGE, prepared, signed_upload
from tests.test_release_migration import (
    TIMES,
    assert_old_columns_unchanged,
    seed_legacy,
    table_snapshot,
)


def stage(core, monkeypatch, *, data=b"first\n\nsecond", filename="notes.txt", mime="text/plain",
          config=None, count=1):
    config, approval, key, event_id, files = prepared(
        core, monkeypatch, data=data, filename=filename, config=config, count=count,
    )
    content_files.schedule_files(core.content, event_id)
    claim = content_files.claim(core.content, event_id, "file:0", files[0]["source_sha256"])
    proof = signed_upload(approval, key, event_id, files[0], claim, data, mime)
    result = content_files.stage_file(core.content, event_id, "file:0", data, mime,
                                      claim["lease_token"], proof)
    return config, approval, key, event_id, files, claim, proof, result


def synthetic_parser(data, filename, mime, **kwargs):
    """Only the external Docker transport is substituted; real text extraction still runs."""
    result = file_parser.parse_document(data, filename, mime, limits=kwargs["limits"])
    return result | {"isolation": "docker_network_none"}


def test_upload_only_stages_and_worker_commits_existing_observation_chain(core, monkeypatch):
    data = b"first\n\nsecond"
    _, _, _, event_id, _, claim, proof, accepted = stage(core, monkeypatch, data=data)
    assert accepted["state"] == "QUEUED" and not accepted["duplicate"]
    assert not core.store.rows("SELECT * FROM observations")
    assert not core.store.rows("SELECT * FROM model_usage")
    row = core.store.one("SELECT * FROM content_items")
    assert image_spool.get(core.store, row, core.clock(), 1024) == (data, "text/plain")
    assert core.content.status(event_id)["pending"]
    assert content_files.stage_file(core.content, event_id, "file:0", data, "text/plain",
                                    claim["lease_token"], proof)["duplicate"]
    calls = []

    def parse(*args, **kwargs):
        assert not core.store.db.in_transaction
        assert kwargs["image"] == IMAGE
        assert kwargs["workspace"] == Path(core.settings.database).parent
        calls.append(args)
        return synthetic_parser(*args, **kwargs)

    assert run(content_files.run_file_once(core.content, parse=parse))
    assert not run(content_files.run_file_once(core.content, parse=parse))
    assert len(calls) == 1
    row = core.store.one("SELECT * FROM content_items")
    assert row["state"] == "SUCCEEDED"
    observation = core.store.one("SELECT * FROM observations")
    document = json.loads(observation["evidence_json"])["document"]
    assert document["filename"] == "notes.txt"
    assert document["file_sha256"] == hashlib.sha256(data).hexdigest()
    assert document["records"][1]["source"] == {"line_start": 3, "line_end": 3}
    assert core.store.one("SELECT * FROM event_dependencies")["source_event_id"] == event_id
    assert not core.store.rows("SELECT * FROM model_usage")
    assert not core.store.rows("SELECT * FROM exploration_requests")
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key=?",
                              (qq_files.STAGED + row["item_id"],))
    assert list(image_spool.directory(core.store).glob("*.raw")) == []
    context = core.content.context(event_id)
    assert context[0]["text"] == "first\nsecond"
    assert context[0]["document"]["partial"] is False
    assert content_files.stage_file(core.content, event_id, "file:0", data, "text/plain",
                                    claim["lease_token"], proof)["duplicate"]


def test_configured_socket_uses_stable_item_id_and_frozen_transport(core, monkeypatch):
    config = PrivateFilesConfig(enabled=True, parser_image=IMAGE,
                                 parser_socket="/run/companion-parser/parser.sock")
    stage(core, monkeypatch, config=config)
    row = core.store.one("SELECT * FROM content_items")
    assert json.loads(row["attachment_json"])["parser_socket"] == config.parser_socket
    calls = []

    def parse(data, filename, mime, **kwargs):
        assert kwargs["request_id"] == row["item_id"]
        assert kwargs["socket_path"] == config.parser_socket
        assert "workspace" not in kwargs
        calls.append(kwargs)
        return synthetic_parser(data, filename, mime, **kwargs)

    monkeypatch.setattr(content_files.file_parser_client, "parse_via_socket", parse)
    monkeypatch.setattr(content_files.file_parser, "parse_isolated",
                        lambda *args, **kwargs: pytest.fail("socket bypassed"))
    assert run(content_files.run_file_once(core.content))
    assert len(calls) == 1
    assert core.store.one("SELECT * FROM content_items")["state"] == "SUCCEEDED"


def test_socket_policy_change_remains_fenced(core, monkeypatch):
    config = PrivateFilesConfig(enabled=True, parser_image=IMAGE,
                                 parser_socket="/run/companion-parser/parser.sock")
    stage(core, monkeypatch, config=config)
    changed = config.model_copy(update={"parser_socket": "/run/changed.sock"})
    assert run(content_files.run_file_once(core.content, config=changed,
        parse=lambda *args, **kwargs: pytest.fail("changed transport reached parser")))
    assert core.store.one("SELECT * FROM content_items")["error_code"] == (
        "PRIVATE_FILE_SNAPSHOT_CHANGED"
    )


def test_socket_lost_response_is_unknown_and_original_is_discarded(core, monkeypatch):
    config = PrivateFilesConfig(enabled=True, parser_image=IMAGE,
                                 parser_socket="/run/companion-parser/parser.sock")
    stage(core, monkeypatch, config=config)

    def lost(data, filename, mime, **kwargs):
        return file_parser._result(data, filename, mime) | {
            "status": "UNAVAILABLE", "error_code": "PARSER_CLEANUP_UNCONFIRMED",
        }

    monkeypatch.setattr(content_files.file_parser_client, "parse_via_socket", lost)
    assert run(content_files.run_file_once(core.content))
    row = core.store.one("SELECT * FROM content_items")
    assert row["state"] == "UNKNOWN"
    assert row["error_code"] == "PARSER_CLEANUP_UNCONFIRMED"
    assert not run(content_files.run_file_once(core.content))
    assert not core.store.rows("SELECT * FROM observations")
    assert not list(image_spool.directory(core.store).glob("*.raw"))


def test_claim_reserves_once_and_checks_current_source_at_each_boundary(core, monkeypatch):
    _, _, _, event_id, files = prepared(core, monkeypatch)
    first = content_files.claim(core.content, event_id, "file:0", files[0]["source_sha256"])
    assert set(first) == {"state", "max_bytes", "lease_token", "lease_until"}
    assert first["lease_token"].startswith("content_lease_")
    with pytest.raises(CoreError, match="PRIVATE_FILE_ALREADY_FETCHING"):
        content_files.claim(core.content, event_id, "file:0", files[0]["source_sha256"])
    assert content_files.check(core.content, event_id, first["lease_token"],
                               files[0]["source_sha256"])["approved"]
    core.store.execute("UPDATE conversations SET reply_revision=reply_revision+1")
    with pytest.raises(CoreError, match="REPLY_SUPERSEDED"):
        content_files.check(core.content, event_id, first["lease_token"], files[0]["source_sha256"])


@pytest.mark.parametrize(("filename", "configuration", "count", "code"), [
    ("program.exe", {}, 1, "PRIVATE_FILE_TYPE_UNSUPPORTED"),
    ("notes.txt", {"max_daily_files": 0}, 1, "PRIVATE_FILE_TRAFFIC_EXHAUSTED"),
    ("notes.txt", {"max_daily_bytes": 1}, 1, "PRIVATE_FILE_TRAFFIC_EXHAUSTED"),
    ("notes.txt", {"max_files_per_event": 1}, 2, "PRIVATE_FILE_COUNT_LIMIT"),
])
def test_unavailable_files_get_explicit_states_without_download(core, monkeypatch, filename,
                                                                configuration, count, code):
    config = PrivateFilesConfig(enabled=True, parser_image=IMAGE, **configuration)
    _, _, _, event_id, _ = prepared(core, monkeypatch, filename=filename,
                                    count=count, config=config)
    rows = content_files.schedule_files(core.content, event_id)
    assert rows[-1]["state"] == "FAILED"
    assert rows[-1]["error_code"] == code
    assert rows[-1]["reserved_bytes"] == 0
    assert content_files.schedule_files(core.content, event_id) == rows


@pytest.mark.parametrize("failure", ["mime", "bytes", "signature", "expired"])
def test_bad_upload_never_creates_an_original(core, monkeypatch, failure):
    data = b"valid text"
    _, approval, key, event_id, files = prepared(core, monkeypatch, data=data)
    claim = content_files.claim(core.content, event_id, "file:0", files[0]["source_sha256"])
    mime = "application/pdf" if failure == "mime" else "text/plain"
    uploaded = b"short" if failure == "bytes" else data
    proof = signed_upload(approval, key, event_id, files[0], claim, uploaded, mime)
    if failure == "signature":
        proof["signature"] = "f" * 128
    if failure == "expired":
        core.clock.advance(21)
    with pytest.raises(CoreError):
        content_files.stage_file(core.content, event_id, "file:0", uploaded, mime,
                                 claim["lease_token"], proof)
    assert not image_spool.directory(core.store).exists()
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key LIKE ?",
                              (qq_files.STAGED + "%",))


def test_changed_retry_conflicts_without_overwriting_original(core, monkeypatch):
    _, approval, key, event_id, files, claim, _, _ = stage(core, monkeypatch, data=b"first")
    altered = b"other"
    proof = signed_upload(approval, key, event_id, files[0], claim, altered)
    with pytest.raises(CoreError, match="IDEMPOTENCY_CONFLICT"):
        content_files.stage_file(core.content, event_id, "file:0", altered, "text/plain",
                                 claim["lease_token"], proof)
    row = core.store.one("SELECT * FROM content_items")
    assert image_spool.get(core.store, row, core.clock(), 100)[0] == b"first"


@pytest.mark.parametrize("change", ["new_input", "privacy", "erase", "stale_lease", "snapshot"])
def test_late_parser_result_cannot_revive_changed_sources(core, monkeypatch, change):
    _, _, _, event_id, _, _, _, _ = stage(core, monkeypatch)

    def parse(*args, **kwargs):
        if change == "new_input":
            core.store.execute("UPDATE conversations SET reply_revision=reply_revision+1")
        elif change == "privacy":
            advance_epoch(core.store)
        elif change == "erase":
            with core.store.transaction():
                core.ledger.expire_events(core.store, [event_id], core.clock())
        elif change == "stale_lease":
            core.store.execute("UPDATE content_items SET lease_token='content_lease_stale'")
        else:
            core.store.execute("UPDATE content_items SET attachment_json='{}'")
        return synthetic_parser(*args, **kwargs)

    assert run(content_files.run_file_once(core.content, parse=parse))
    assert not core.store.rows("SELECT * FROM observations")
    assert core.store.one("SELECT state FROM content_items")["state"] != "SUCCEEDED"
    assert list(image_spool.directory(core.store).glob("*.raw")) == []
    if change == "erase":
        row = core.store.one("SELECT * FROM content_items")
        assert row["state"] == "ERASED" and row["attachment_json"] == "{}"
        assert row["media_sha256"] == "" and row["fingerprint"] == ""


@pytest.mark.parametrize("damaged", ["bytes", "proof"])
def test_damaged_staged_input_is_rejected_before_parser(core, monkeypatch, damaged):
    stage(core, monkeypatch)
    if damaged == "bytes":
        path = next(image_spool.directory(core.store).glob("*.raw"))
        original = path.read_bytes()
        path.write_bytes(original[:-1] + b"X")
    else:
        core.store.execute("UPDATE runtime_meta SET value='{' WHERE key LIKE ?",
                           (qq_files.STAGED + "%",))
    assert run(content_files.run_file_once(core.content,
        parse=lambda *args, **kwargs: pytest.fail("tampered original reached parser")))
    assert core.store.one("SELECT error_code FROM content_items")["error_code"] == (
        "PRIVATE_FILE_ORIGINAL_CHANGED")


def test_lost_upload_and_unavailable_parser_are_terminal_without_repeat(core, monkeypatch):
    stage(core, monkeypatch)
    calls = []

    def unavailable(data, filename, mime, **kwargs):
        calls.append(1)
        return file_parser._result(data, filename, mime) | {
            "status": "UNAVAILABLE", "error_code": "PARSER_SANDBOX_UNAVAILABLE",
        }

    assert run(content_files.run_file_once(core.content, parse=unavailable))
    assert not run(content_files.run_file_once(core.content, parse=unavailable))
    assert calls == [1]
    assert core.store.one("SELECT state FROM content_items")["state"] == "UNKNOWN"


def test_empty_partial_pdf_preserves_unknown_text_and_no_ocr(core, monkeypatch):
    data = b"%PDF-synthetic-scanned-page"
    stage(core, monkeypatch, data=data, filename="scan.pdf", mime="application/pdf")

    def scanned(data, filename, mime, **kwargs):
        value = file_parser._result(data, filename, mime)
        empty = hashlib.sha256(b"").hexdigest()
        return value | {"status": "PARTIAL", "partial": True,
                        "issues": ["PAGE_WITHOUT_TEXT_LAYER"], "engine": "pypdf/synthetic",
                        "isolation": "docker_network_none", "records": [{
                            "kind": "page", "source": {"page": 1}, "text": "",
                            "sha256": empty, "truncated": False,
                        }]}

    assert run(content_files.run_file_once(core.content, parse=scanned))
    item = core.store.one("SELECT state,error_code FROM content_items")
    assert item == {"state": "SUCCEEDED", "error_code": "PRIVATE_FILE_TEXT_UNAVAILABLE"}
    observation = core.store.one("SELECT result_status,evidence_json FROM observations")
    assert observation["result_status"] == "UNKNOWN"
    evidence = json.loads(observation["evidence_json"])["document"]
    assert evidence["partial"] and evidence["ocr"] == "not_attempted"


def test_task_cancellation_fences_result_and_discards_original(core, monkeypatch):
    stage(core, monkeypatch)

    async def scenario():
        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(asyncio, "to_thread", cancelled)
        with pytest.raises(asyncio.CancelledError):
            await content_files.run_file_once(core.content)

    run(scenario())
    assert core.store.one("SELECT state,error_code FROM content_items") == {
        "state": "UNKNOWN", "error_code": "PRIVATE_FILE_PROCESSOR_CANCELLED",
    }
    assert list(image_spool.directory(core.store).glob("*.raw")) == []


def test_erasure_replay_removes_copied_file_spool_and_metadata(core, monkeypatch, tmp_path):
    _, _, _, event_id, _, _, _, _ = stage(core, monkeypatch)
    key, archive = Fernet.generate_key(), tmp_path / "files.enc"
    destination = tmp_path / "restore.db"
    backup(core.settings.database, archive, key)
    copied = destination.parent / (destination.name + ".owner-images")
    shutil.copytree(image_spool.directory(core.store), copied)
    with core.store.transaction():
        core.ledger.expire_events(core.store, [event_id], core.clock())
    restore(archive, destination, key, core.settings.erasure_ledger)
    assert list(copied.glob("*.raw")) == []
    with Core(core.settings.model_copy(update={"database": str(destination)}),
              clock=core.clock) as recovered:
        row = recovered.store.one("SELECT * FROM content_items")
        assert row["state"] == "ERASED" and row["attachment_json"] == "{}"
        assert not recovered.store.one("SELECT 1 FROM runtime_meta WHERE key LIKE ?",
                                       (qq_files.STAGED + "%",))
        assert not run(content_files.run_file_once(recovered.content,
            parse=lambda *args, **kwargs: pytest.fail("erased file replayed")))
    assert list(copied.glob("*.raw")) == []


def test_file_migration_preserves_old_content_and_refuses_lossy_downgrade(tmp_path):
    database, _, _ = seed_legacy(tmp_path, "0003_memory_index")
    migrate(str(database), "0027_owner_console")
    with closing(Store(str(database))) as store:
        store.insert("content_items", item_id="content_" + "a" * 32, owner_event_id="evt_keep",
                     kind="qq_image", slot=0, state="UNKNOWN", captured_privacy_epoch=3,
                     reserved_bytes=1000, deadline=TIMES["keep"], created_at=TIMES["keep"],
                     updated_at=TIMES["keep"], media_sha256="b" * 64)
        old = table_snapshot(store, ("events", "content_items"))
    migrate(str(database), "0028_private_files")
    migrate(str(database), "0028_private_files")
    with closing(Store(str(database))) as store:
        row = store.one("SELECT * FROM content_items")
        assert row.pop("attachment_json") == "{}"
        assert_old_columns_unchanged(store, old)
        assert store.rows("PRAGMA foreign_key_check") == []
        store.execute("UPDATE content_items SET kind='qq_file'")
    with pytest.raises(RuntimeError, match="PRIVATE_FILES_DOWNGRADE_REQUIRES"):
        command.downgrade(config_for(database), "0027_owner_console")


def test_disabled_files_and_frozen_limits_are_not_silently_changed(core, monkeypatch):
    assert not run(content_files.run_file_once(core.content))
    stage(core, monkeypatch)
    changed = core.settings.private_files.model_copy(update={
        "limits": replace(core.settings.private_files.limits, max_text_chars=100),
    })
    assert run(content_files.run_file_once(core.content, config=changed,
        parse=lambda *args, **kwargs: pytest.fail("changed limits reached parser")))
    assert core.store.one("SELECT error_code FROM content_items")["error_code"] == (
        "PRIVATE_FILE_SNAPSHOT_CHANGED")

"""Offline catalog import rehearsal; runnable without the Bridge application."""

import argparse
import base64
import binascii
import json
import os
import re
import stat
import tomllib
from contextlib import closing
from pathlib import Path

import psycopg
from pydantic import ValidationError

from companion import meme_assets
from companion.config import Settings
from companion.contracts import CoreError
from companion.postgres import Database
from companion.privacy import ErasureLedger, epoch
from companion.store import Store
from companion_delivery_protocol.authority import validate_authority_ref
from companion_meme_protocol import digest, strict_json, validate_bytes

MAX_ROWS, MAX_BYTES, MAX_MANIFEST = 4096, 64 * 1024 * 1024, 100 * 1024 * 1024
SHA = re.compile(r"[a-f0-9]{64}")


def _linked(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return True
    if os.name == "nt":
        try:
            return bool(info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except AttributeError as error:
            raise ValueError("MEME_MIGRATION_FILE_INVALID") from error
    return False


def regular_file(path, maximum=None):
    path = Path(path).absolute()
    if (any(_linked(p) for p in (path, *path.parents))
        or not stat.S_ISREG(path.stat().st_mode)
        or maximum is not None and path.stat().st_size > maximum):
        raise ValueError("MEME_MIGRATION_FILE_INVALID")
    return path


def checked_hash(value):
    if (not isinstance(value, dict) or not isinstance(value.get("sha256"), str)
        or not SHA.fullmatch(value["sha256"])
        or digest({k: v for k, v in value.items() if k != "sha256"}) != value["sha256"]):
        raise ValueError("MEME_MANIFEST_HASH_INVALID")


def read_manifest(path):
    with regular_file(path, MAX_MANIFEST).open("rb") as stream:
        raw = stream.read(MAX_MANIFEST + 1)
    if len(raw) > MAX_MANIFEST:
        raise ValueError("MEME_MANIFEST_LIMIT")
    manifest = strict_json(raw)
    checked_hash(manifest)
    if (set(manifest) != {"schema_version", "custody", "assets", "sha256"}
        or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
        or not isinstance(manifest["assets"], list) or len(manifest["assets"]) > MAX_ROWS):
        raise ValueError("MEME_MANIFEST_INVALID")
    custody = manifest["custody"]
    if (not isinstance(custody, dict) or set(custody) != {
        "identity", "capture_public_key", "authority", "journal_head"}
        or not isinstance(custody["identity"], list) or len(custody["identity"]) != 4
        or not all(isinstance(v, str) for v in custody["identity"])
        or not all(re.fullmatch(r"[1-9][0-9]{4,19}", v) for v in custody["identity"][:2])
        or custody["identity"][0] == custody["identity"][1]
        or custody["identity"][2] != "onebot-direct:FriendMessage:" + custody["identity"][0]
        or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", custody["identity"][3])
        or not isinstance(custody["capture_public_key"], str)
        or not SHA.fullmatch(custody["capture_public_key"])
        or not isinstance(custody["journal_head"], str)
        or not SHA.fullmatch(custody["journal_head"])):
        raise ValueError("MEME_MANIFEST_CUSTODY_INVALID")
    validate_authority_ref(custody["authority"])
    seen, size, items = set(), 0, []
    for item in manifest["assets"]:
        checked_hash(item)
        if (set(item) != {"asset_sha256", "source_sha256", "active", "metadata", "mime",
                         "data_base64", "sha256"}
            or any(not isinstance(item[k], str) or not SHA.fullmatch(item[k])
                   for k in ("asset_sha256", "source_sha256"))
            or type(item["active"]) is not bool):
            raise ValueError("MEME_MANIFEST_ITEM_INVALID")
        meta = meme_assets.metadata(item["metadata"])
        if meme_assets.source_digest(meta) != item["source_sha256"]:
            raise ValueError("MEME_MANIFEST_SOURCE_INVALID")
        key = meme_assets.asset_id(custody, item)
        if key in seen:
            raise ValueError("MEME_MANIFEST_DUPLICATE")
        seen.add(key)
        data = None
        if item["active"]:
            if (not isinstance(item["data_base64"], str)
                or len(item["data_base64"]) > 5592408):
                raise ValueError("MEME_MANIFEST_MEDIA_INVALID")
            data = base64.b64decode(item["data_base64"], validate=True)
            media = validate_bytes(data)
            if (media is None or media.sha256 != item["asset_sha256"]
                or media.mime_type != item["mime"]
                or base64.b64encode(data).decode() != item["data_base64"]):
                raise ValueError("MEME_MANIFEST_MEDIA_INVALID")
            size += len(data)
            if size > MAX_BYTES:
                raise ValueError("MEME_MANIFEST_LIMIT")
        elif item["data_base64"] is not None or item["mime"] is not None:
            raise ValueError("MEME_MANIFEST_MEDIA_INVALID")
        items.append((item, data))
    return manifest, items


def _ledger(store, path):
    ledger = ErasureLedger(regular_file(path))
    records = ledger.read()
    identity = store.one("SELECT value FROM runtime_meta WHERE key='ledger_id'")
    if not identity or identity["value"] != records[0]["ledger_id"]:
        raise CoreError("ERASURE_LEDGER_MISMATCH", 503)
    return ledger, digest(records)


def _projection(store):
    return {
        "assets": store.rows("SELECT * FROM meme_assets ORDER BY asset_id"),
        "sources": store.rows("SELECT * FROM meme_asset_sources ORDER BY asset_id,event_id,"
                              "caption_job_id"),
    }


def _apply(store, settings, manifest, items):
    custody = manifest["custody"]
    if custody["identity"][:2] != [settings.owner_qq_id, settings.bot_qq_id]:
        raise ValueError("MEME_ASSET_CUSTODY_MISMATCH")
    results = []
    for item, data in items:
        sources, withdrawn = meme_assets.provenance(store, settings, custody, item, data)
        results.append(meme_assets.store_original(store, custody, item, data, sources, withdrawn))
    meme_assets.check_capacity(store, max_rows=MAX_ROWS, max_bytes=MAX_BYTES)
    return results


def run(settings, manifest_path, *, operation="dry-run"):
    if operation not in {"dry-run", "import", "reconcile"}:
        raise ValueError("MEME_MIGRATION_OPERATION_INVALID")
    manifest, items = read_manifest(manifest_path)
    # Preflight the existing target read-only: missing/corrupt ledgers never create
    # a database, initialize a new Ledger or modify schema during a rehearsal.
    with Database(settings.database, max_connections=1) as database, closing(
        Store(database, settings.state_directory),
    ) as store:
        with store.read_snapshot():
            _ledger(store, settings.erasure_ledger)
            if not store.one("SELECT 1 FROM pg_class WHERE "
                "relnamespace=current_schema()::regnamespace AND relname='meme_asset_sources' "
                "AND relkind IN ('r','p')"):
                raise ValueError("MEME_ASSET_SCHEMA_REQUIRED")
        return _run(store, settings, manifest, items, operation)


def _run(store, settings, manifest, items, operation):
    with store.transaction(control_write=True, rollback=operation != "import"):
        before = _projection(store)
        ledger, ledger_hash = _ledger(store, settings.erasure_ledger)
        ledger.replay(store)
        if _ledger(store, settings.erasure_ledger)[1] != ledger_hash:
            raise ValueError("MEME_MIGRATION_PRIVACY_CHANGED")
        version = epoch(store)
        results = _apply(store, settings, manifest, items)
        if epoch(store) != version or _ledger(store, settings.erasure_ledger)[1] != ledger_hash:
            raise ValueError("MEME_MIGRATION_PRIVACY_CHANGED")
        return {"operation": operation, "manifest_sha256": manifest["sha256"],
                "matches": before == _projection(store), "items": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("operation", choices=("dry-run", "import", "reconcile"))
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        config = tomllib.loads(regular_file(args.config, 1024 * 1024).read_text("utf-8"))
        result = run(Settings.model_validate(config), args.manifest, operation=args.operation)
    except (CoreError, ValueError, KeyError, TypeError, OSError, psycopg.Error,
            binascii.Error, ValidationError):
        print(json.dumps({"error": "MEME_MIGRATION_REFUSED"}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

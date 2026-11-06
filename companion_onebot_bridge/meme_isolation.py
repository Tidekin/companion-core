"""Retire a stopped Bridge catalog while retaining its original responsibilities.

The operator first exports and reconciles the same catalog into Core, and binds every
retired physical SHA in Core's exclusion policy. This command grants no Core authority.
The existing owner journal fences old binaries/backups; its original receipts are retained.
"""

import argparse
import base64
import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from companion_meme_protocol import canonical, digest, source_evidence, strict_json

from .config import BridgeConfig
from .instance_lock import acquire
from .meme_export import MAX_MANIFEST, _file, _ReadonlyRuntime
from .meme_retirement import CONTENT, SHA, _accounting, _inventory
from .owner_handoff import closed

KIND = "MEME_CATALOG_RETIRED"
# These are content projections only. Receipt identities, states, usage and pins stay put.
CLEAR = {
    "meme_assets": {"data": None, "metadata": "{}", "active": 0},
    "meme_offers": {"intent": "", "category": None, "ids": "[]"},
    "meme_runtime_offers": {"offer_json": "null"},
    "meme_runtime_sources": {"claim_json": "{}"},
    "meme_runtime_semantic_offers": {"query_binding": None, "offer_json": None},
    "meme_caption_cache": {"identity_json": None, "result_json": None},
    "meme_caption_bindings": {"context_json": "{}", "outcome_json": None},
    "meme_semantic_vectors": {"vector_json": None},
    "meme_semantic_offers": {"proof_json": "{}"},
}


def validate_record(value, authority, state):
    if (state != "STOPPED" or not isinstance(value, dict) or set(value) != {
        "version", "authority", "manifest_sha256", "excluded_sha256", "original_sha256",
        "retained_sha256"} or type(value["version"]) is not int or value["version"] != 1
        or value["authority"] != authority
        or any(not isinstance(value[k], str) or not SHA.fullmatch(value[k])
               for k in ("manifest_sha256", "original_sha256", "retained_sha256"))
        or not isinstance(value["excluded_sha256"], list)
        or not 1 <= len(value["excluded_sha256"]) <= 4096
        or any(not isinstance(v, str) or not SHA.fullmatch(v) for v in value["excluded_sha256"])
        or value["excluded_sha256"] != sorted(set(value["excluded_sha256"]))):
        raise ValueError("MEME_RETIREMENT_RECORD_INVALID")
    return value


def _projection(db, excluded, *, content=False):
    result = {}
    # New Core transport may add bindings and pins for other physical images.
    for table in (*CONTENT, "meme_semantic_assets", "meme_semantic_config",
                  "meme_runtime_results", "meme_runtime_bindings", "meme_pins"):
        columns = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
        if not content:
            columns = [c for c in columns if c not in CLEAR.get(table, {})]
        predicate, args = "", ()
        if table in {"meme_pins", "meme_runtime_bindings"}:
            field = "sha" if table == "meme_pins" else "candidate_id"
            predicate = f" WHERE {field} IN (SELECT value FROM json_each(?))"
            args = (canonical(excluded),)
        rows = db.execute(f"SELECT {','.join(columns)} FROM {table}" + predicate, args)
        material = [[{"blob_sha256": hashlib.sha256(v).hexdigest(), "bytes": len(v)}
                     if isinstance(v, bytes) else v for v in row] for row in rows]
        result[table] = {"columns": columns, "rows": sorted(material, key=canonical)}
    return digest(result)


def verify_projection(db, value):
    for table, fields in CLEAR.items():
        predicate = " OR ".join(f"{column} IS NOT ?" for column in fields)
        if db.execute(f"SELECT 1 FROM {table} WHERE {predicate} LIMIT 1",
                      tuple(fields.values())).fetchone():
            raise ValueError("MEME_RETIRED_CONTENT_RESTORED")
    if _projection(db, value["excluded_sha256"]) != value["retained_sha256"]:
        raise ValueError("MEME_RETIRED_RESPONSIBILITY_CHANGED")


def _clear(db):
    for table, fields in CLEAR.items():
        assignments = ",".join(f"{column}=?" for column in fields)
        db.execute(f"UPDATE {table} SET {assignments}", tuple(fields.values()))


def _manifest(path):
    source = _file(path)
    if not 0 < source.stat().st_size <= MAX_MANIFEST:
        raise ValueError("MEME_MANIFEST_LIMIT")
    value = strict_json(source.read_bytes())
    if (not isinstance(value, dict)
        or set(value) != {"schema_version", "custody", "assets", "sha256"}
        or value["schema_version"] != 1
        or digest({k: v for k, v in value.items() if k != "sha256"}) != value["sha256"]
        or not isinstance(value["assets"], list) or not 1 <= len(value["assets"]) <= 4096):
        raise ValueError("MEME_RETIREMENT_MANIFEST_INVALID")
    return value


def _matches(db, config, original, manifest):
    custody = manifest["custody"]
    if custody != {"identity": [config.owner_qq_id, config.bot_qq_id, config.origin,
                                config.protocol_epoch],
                   "capture_public_key": config.capture_public_key,
                   "authority": config.owner_authority, "journal_head": original["journal_head"]}:
        raise ValueError("MEME_RETIREMENT_CUSTODY_CHANGED")
    from .meme_catalog import MemeCatalog

    expected = {}
    for item in manifest["assets"]:
        sha = item["asset_sha256"]
        if sha in expected or not SHA.fullmatch(sha):
            raise ValueError("MEME_RETIREMENT_MANIFEST_INVALID")
        if digest({k: v for k, v in item.items() if k != "sha256"}) != item["sha256"]:
            raise ValueError("MEME_RETIREMENT_MANIFEST_INVALID")
        expected[sha] = item
    rows = db.execute("SELECT sha,data,metadata,active FROM meme_assets").fetchall()
    if {r[0] for r in rows} != set(expected):
        raise ValueError("MEME_RETIREMENT_INVENTORY_CHANGED")
    for sha, data, metadata, active in rows:
        item = expected[sha]
        if (item["active"] is not bool(active) or item["metadata"] != strict_json(metadata)
            or item["source_sha256"] != digest(source_evidence(MemeCatalog.metadata(metadata)))
            or (base64.b64encode(data).decode() if data is not None else None)
            != item["data_base64"]):
            raise ValueError("MEME_RETIREMENT_INVENTORY_CHANGED")
    excluded = sorted(expected)
    if db.execute("SELECT 1 FROM meme_pins WHERE sha IS NULL OR sha NOT IN "
                  "(SELECT value FROM json_each(?)) LIMIT 1", (canonical(excluded),)).fetchone():
        raise ValueError("MEME_RETIREMENT_PIN_UNATTRIBUTED")
    return excluded


def retire(config, manifest_path):
    """One offline, idempotent, stopped operation under the original instance lock."""
    manifest = _manifest(manifest_path)
    directory = Path(config.data_dir).absolute()
    database, lock = (_file(directory / name) for name in ("spool.sqlite3", "instance.lock"))
    with acquire(lock, existing_only=True), closing(sqlite3.connect(
        database.as_uri() + "?mode=rw", uri=True)) as db:
        runtime = _ReadonlyRuntime(db, config, directory)
        records = runtime.verify(active=False)
        if (runtime.row()["state"] != "STOPPED"
            or runtime.stop_path.read_bytes() != b"OWNER_STOP\n"):
            raise ValueError("MEME_RETIREMENT_STOP_REQUIRED")
        prior = [r["data"] for r in records if r["kind"] == KIND]
        if prior:
            if prior[0]["manifest_sha256"] != manifest["sha256"]:
                raise ValueError("MEME_RETIREMENT_CONFLICT")
            return {"state": "RETIRED", "duplicate": True, **prior[0]}
        references = [r["data"]["authority"] for r in records
                      if r["kind"] in {"OWNER_BOUND", "OWNER_REBOUND"}]
        closed(runtime, records, references)
        issues = Counter()
        _inventory(db, issues)
        if issues:
            raise ValueError("MEME_RETIREMENT_SCHEMA_UNVERIFIED")
        _accounting(db, issues)
        # Accepted UNKNOWN downloads remain UNKNOWN, with all their original counters.
        # A pending result or model call must finish its existing accounting first.
        if (set(issues) - {"DISCOVERY_ACCOUNTING_UNSETTLED"}
            or db.execute("SELECT 1 FROM meme_runtime_results WHERE state NOT IN "
                          "('SETTLED','UNKNOWN') LIMIT 1").fetchone()):
            raise ValueError("MEME_RETIREMENT_ACCOUNTING_UNVERIFIED")
        for state, body in db.execute("SELECT state,body_json FROM meme_runtime_results"):
            if strict_json(body)["outcome"] != state:
                raise ValueError("MEME_RETIREMENT_ACCOUNTING_UNVERIFIED")
        db.execute("BEGIN IMMEDIATE")
        try:
            original = runtime.row()
            excluded = _matches(db, config, original, manifest)
            value = {"version": 1, "authority": config.owner_authority,
                     "manifest_sha256": manifest["sha256"], "excluded_sha256": excluded,
                     "original_sha256": _projection(db, excluded, content=True),
                     "retained_sha256": _projection(db, excluded)}
            validate_record(value, config.owner_authority, "STOPPED")
            runtime.append(KIND, value)
            _clear(db)
            verify_projection(db, value)
            db.commit()
        except BaseException:
            db.rollback()
            raise  # A committed journal with a rolled-back projection fails closed on restart.
        runtime.verify(active=False)
        return {"state": "RETIRED", "duplicate": False, **value}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = retire(BridgeConfig.load(args.config), args.manifest)
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
        print(json.dumps({"error": "MEME_RETIREMENT_REFUSED"}))
        return 1
    # The full receipt is an operator artifact, with no private text or asset bytes.
    print(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

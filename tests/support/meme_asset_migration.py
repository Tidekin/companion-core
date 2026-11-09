import base64
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from companion.postgres import Database
from companion.store import Store, encode
from companion_meme_sources import Metadata
from companion_onebot_bridge.meme_catalog import MemeCatalog
from companion_onebot_bridge.meme_library import MemeLibrary
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.postgres_fixture import logical_state
from tests.support.meme_core import body, caption_environment

ROOT = Path(__file__).resolve().parents[2]


def config_file(path, values):
    # These synthetic configs use only scalar fields plus the existing authority.
    lines = []
    for key, value in values.items():
        if isinstance(value, dict):
            literal = "{ " + ", ".join(k + " = " + json.dumps(v)
                                      for k, v in value.items()) + " }"
        else:
            scalar = value.get_secret_value() if isinstance(value, SecretStr) else value
            literal = json.dumps(scalar)
        lines.append(key + " = " + literal)
    path.write_text("\n".join(lines) + "\n", "utf-8")
    path.chmod(0o600)
    return path


def cli(module, *args, ok=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPANION_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-B", "-m", module, *map(str, args)],
                            cwd=ROOT, env=env, capture_output=True, text=True,
                            encoding="utf-8", timeout=45)
    assert (result.returncode == 0) == ok, result.stdout + result.stderr
    assert not result.stderr, result.stderr
    return json.loads(result.stdout)


def logical_database(path):
    if isinstance(path, SecretStr):
        with Database(path) as database, closing(
            Store(database, ROOT / '.local/test-temp'),
        ) as store:
            return logical_state(store)
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        return tuple(db.iterdump())


def original_work(store):
    return {name: store.rows("SELECT * FROM " + name + " ORDER BY record_sequence") for name in (
        "jobs", "job_sources", "meme_captions", "meme_discovery_reservations",
        "model_usage", "model_admissions", "outbox",
    )}


@pytest.fixture
def bundle(core, tmp_path, monkeypatch):
    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, _, client, config, grant, now, attempt = values
            payload = body(core, event, attempt, now)
            response = await client.post("/internal/v1/memes/captions", json=payload)
            assert response.status_code == 200, response.text
            return event, config, grant, payload, response.json()["job_id"]

    event, config, grant, payload, job = run(scenario())
    bridge = replace(config, data_dir=str(tmp_path / "bridge"),
                     capture_signing_key=grant.key.private_bytes_raw().hex(),
                     owner_authority=grant.authority, meme_enabled=True)
    proof = payload["source_evidence"]
    metadata = Metadata("like", "合成图像的未信任目录描述", ("抱抱",), ("comfort",), (),
                        **{key: proof[key] for key in (
                            "source_page", "asset_url", "license", "usage_basis", "fetched_at",
                            "revision")}, acquisition_json=encode(proof["acquisition"]))
    data = base64.b64decode(payload["asset"]["data_base64"])
    with Spool(bridge) as spool:
        runtime = OwnerRuntime(spool)
        catalog = MemeCatalog(spool.db, MemeLibrary(tmp_path, enabled=True))
        catalog.add(data, metadata, check=lambda: None)
        # Unrelated private transport body must never enter the export.
        spool.db.execute("CREATE TABLE unrelated_private(body TEXT)")
        spool.db.execute("INSERT INTO unrelated_private VALUES('UNRELATED_PRIVATE_CANARY')")
        spool.db.commit()
        runtime.stop()
    bridge_config = config_file(tmp_path / "bridge.toml", {
        key: getattr(bridge, key) for key in (
            "owner_qq_id", "bot_qq_id", "bridge_token", "onebot_token", "protocol_epoch",
            "data_dir", "capture_signing_key", "owner_authority", "meme_enabled")})
    core_config = config_file(tmp_path / "core.toml", {
        "owner_qq_id": core.settings.owner_qq_id, "bot_qq_id": core.settings.bot_qq_id,
        "bridge_token": core.settings.bridge_token.get_secret_value(),
        "database": core.settings.database, "state_directory": core.settings.state_directory,
        "erasure_ledger": core.settings.erasure_ledger})
    manifest = tmp_path / "catalog.json"
    return SimpleNamespace(core=core, bridge=bridge, bridge_config=bridge_config,
                           core_config=core_config, manifest=manifest, event=event,
                           payload=payload, job=job, metadata=metadata, data=data)


def check_entrypoint_import_isolation(module, blocked):
    script = """
import importlib, importlib.abc, sys
sys.path.insert(0, sys.argv[1])
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] == sys.argv[3]:
            raise AssertionError('cross-package import')
sys.meta_path.insert(0, Guard())
importlib.import_module(sys.argv[2])
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(ROOT), module, blocked],
                            cwd=ROOT, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stderr

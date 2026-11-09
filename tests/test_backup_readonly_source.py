"""Real read-only source permissions, committed WAL, and private plaintext cleanup."""

import os
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from cryptography.fernet import Fernet
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import SecretStr

from companion import backup as backup_module
from companion import operator_config
from companion.config import Settings
from companion.contracts import CoreError
from tests.postgres_fixture import isolated_database


def windows_sid():
    result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                            capture_output=True, check=True, timeout=10)
    return re.search(rb"S-1-[0-9-]+", result.stdout).group().decode("ascii")


def icacls(*args):
    subprocess.run(["icacls", *map(str, args)], capture_output=True, check=True, timeout=10)


def assert_private_directory(path, acl_file):
    if os.name == "nt":
        # Python >=3.12.4 implements mkdir(mode=0o700) with a protected DACL.
        # Inspect SIDs rather than localized account names. SYSTEM/Admin retain OS access.
        icacls(path, "/save", acl_file)
        acl = acl_file.read_text("utf-16-le").lstrip("\ufeff")
        allowed = set(re.findall(r"\(A;[^()]*;;;([^)]+)\)", acl))
        # OW is the owner's rights SID used by Python's protected mkdir DACL.
        assert {windows_sid(), "OW"} & allowed
        assert allowed <= {windows_sid(), "OW", "SY", "BA"}
        assert re.search(r"^D:P", acl, flags=re.MULTILINE)
    else:
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert path.stat().st_uid == os.geteuid()


def readonly_database(database):
    options = conninfo_to_dict(database.get_secret_value()).get("options", "")
    return SecretStr(make_conninfo(database.get_secret_value(),
                                  options=options + " -c default_transaction_read_only=on"))


def read_archive(archive, key, directory):
    """Inspect the original dump without applying runtime restore fences."""
    directory.mkdir(mode=0o700)
    snapshot = directory / "original.dump"
    snapshot.write_bytes(Fernet(key).decrypt(archive.read_bytes()))
    with isolated_database() as database:
        with backup_module._client(database, directory) as (db, environment):
            backup_module._load_archive(snapshot, environment)
            return db.execute("SELECT value FROM facts ORDER BY value").fetchall()


@pytest.mark.parametrize("failure", [None, "encrypt", "fsync"])
def test_readonly_source_committed_wal_and_private_plaintext_cleanup(
    tmp_path, monkeypatch, failure, empty_database,
):
    encrypted_dir = tmp_path / "encrypted-only"
    encrypted_dir.mkdir(mode=0o700)
    created, plaintext_seen = [], []
    temporary_directory = tempfile.TemporaryDirectory

    @contextmanager
    def observe_directory(*args, **kwargs):
        with temporary_directory(*args, **kwargs) as directory:
            path = Path(directory)
            created.append(path)
            assert path.parent == encrypted_dir
            assert_private_directory(path, tmp_path / "private-acl.txt")
            yield directory

    monkeypatch.setattr(tempfile, "TemporaryDirectory", observe_directory)
    encrypt = Fernet.encrypt

    def observe_plaintext(cipher, value):
        snapshot = created[0] / "snapshot.dump"
        assert snapshot.is_file() and snapshot.read_bytes() == value
        assert value.startswith(b"PGDMP")
        assert_private_directory(created[0], tmp_path / "plaintext-acl.txt")
        plaintext_seen.append(True)
        if failure == "encrypt":
            raise RuntimeError("synthetic encryption failure")
        return encrypt(cipher, value)

    monkeypatch.setattr(Fernet, "encrypt", observe_plaintext)
    if failure == "fsync":
        def fail_fsync(_):
            raise OSError("synthetic destination fsync failure")
        monkeypatch.setattr(backup_module.os, "fsync", fail_fsync)

    database = readonly_database(empty_database)
    archive, key = encrypted_dir / "backup.enc", Fernet.generate_key()
    with psycopg.connect(empty_database.get_secret_value(), autocommit=True) as setup:
        setup.execute("CREATE TABLE facts (value TEXT)")
        setup.execute("INSERT INTO facts VALUES ('committed-in-wal')")
    with psycopg.connect(empty_database.get_secret_value()) as writer:
        writer.execute("INSERT INTO facts VALUES ('uncommitted')")
        if failure:
            expected = RuntimeError if failure == "encrypt" else OSError
            with pytest.raises(expected, match="synthetic"):
                backup_module.backup(database, archive, key)
        else:
            backup_module.backup(database, archive, key)
        ciphertext = archive.read_bytes()
        with pytest.raises(CoreError, match="BACKUP_DESTINATION_EXISTS"):
            backup_module.backup(database, archive, key)
        assert archive.read_bytes() == ciphertext
        assert writer.execute("SELECT value FROM facts ORDER BY value").fetchall() == [
            ("committed-in-wal",), ("uncommitted",)]
        writer.rollback()
    with psycopg.connect(database.get_secret_value()) as reader:
        assert reader.execute("SHOW transaction_read_only").fetchone() == ("on",)
        assert reader.execute("SELECT value FROM facts").fetchall() == [("committed-in-wal",)]
    assert len(created) == 1 and plaintext_seen == [True]
    assert not created[0].exists()
    assert sorted(path.name for path in encrypted_dir.iterdir()) == ["backup.enc"]
    if failure == "encrypt":
        assert archive.read_bytes() == b""
    else:
        assert not ciphertext.startswith(b"PGDMP")
        assert read_archive(archive, key, tmp_path / "inspection") == [("committed-in-wal",)]


def test_cli_backup_reads_native_source_without_runtime_bootstrap(tmp_path, empty_database):
    with psycopg.connect(empty_database.get_secret_value()) as db:
        db.execute("CREATE TABLE facts (value TEXT)")
        db.execute("INSERT INTO facts VALUES ('committed-before-stop')")
    database = readonly_database(empty_database)
    key = Fernet.generate_key()
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("COMPANION_")}
    env.update(COMPANION_CONFIG=str(tmp_path / "absent.toml"),
               COMPANION_DATABASE=database.get_secret_value(), COMPANION_MODE="demo",
               COMPANION_STATE_DIRECTORY=str(tmp_path / "unused-state"),
               COMPANION_ERASURE_LEDGER=str(tmp_path / "unused-ledger.jsonl"),
               COMPANION_OWNER_QQ_ID="10001", COMPANION_BOT_QQ_ID="10002",
               COMPANION_BRIDGE_TOKEN="synthetic-stopped-backup-token",
               COMPANION_BACKUP_KEY=key.decode())
    archive = tmp_path / "cli-backup.enc"
    subprocess.run([sys.executable, "-B", "-m", "companion", "backup", str(archive)],
                   env=env, capture_output=True, check=True, timeout=30)
    assert not (tmp_path / "unused-state").exists()
    assert not (tmp_path / "unused-ledger.jsonl").exists()
    assert read_archive(archive, key, tmp_path / "inspection") == [("committed-before-stop",)]
    with psycopg.connect(database.get_secret_value()) as db:
        assert db.execute("SELECT value FROM facts").fetchall() == [("committed-before-stop",)]
        assert db.execute("SELECT tablename FROM pg_tables WHERE "
                          "schemaname=current_schema()").fetchall() == [("facts",)]


@pytest.mark.parametrize("statement", ["INSERT INTO facts VALUES ('forbidden')",
                                      "CREATE TABLE forbidden(value TEXT)"])
@pytest.mark.parametrize("readonly", ["on", "true", "1"])
def test_native_readonly_source_denies_mutation_before_backup(
    tmp_path, empty_database, statement, readonly,
):
    with psycopg.connect(empty_database.get_secret_value()) as db:
        db.execute("CREATE TABLE facts (value TEXT)")
        db.execute("INSERT INTO facts VALUES ('retained')")
    options = conninfo_to_dict(empty_database.get_secret_value()).get("options", "")
    database = SecretStr(make_conninfo(empty_database.get_secret_value(),
        options=options + " -c default_transaction_read_only=" + readonly))
    with psycopg.connect(database.get_secret_value()) as db:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            db.execute(statement)
        db.rollback()
        assert db.execute("SELECT value FROM facts").fetchall() == [("retained",)]
    archive, key = tmp_path / "backup.enc", Fernet.generate_key()
    backup_module.backup(database, archive, key)
    assert read_archive(archive, key, tmp_path / "inspection") == [("retained",)]


def test_settings_load_keeps_runtime_overrides_and_deployment_custody(core, monkeypatch, tmp_path):
    password = "synthetic-configuration-layering-token"
    core.settings.control_token = SecretStr(password)
    operator_config.update(
        core.store, core.settings,
        operator_config.ConfigurationChange(
            expected_version=0, changes={"max_output_tokens": 1024},
        ),
        "o1-configuration-layering", core.clock(),
    )
    for name in tuple(os.environ):
        if name.startswith("COMPANION_"):
            monkeypatch.delenv(name)
    values = {
        "CONFIG": str(tmp_path / "absent.toml"),
        "DATABASE": core.settings.database.get_secret_value(),
        "STATE_DIRECTORY": core.settings.state_directory,
        "ERASURE_LEDGER": core.settings.erasure_ledger, "MODE": "demo",
        "OWNER_QQ_ID": core.settings.owner_qq_id, "BOT_QQ_ID": core.settings.bot_qq_id,
        "BRIDGE_TOKEN": core.settings.bridge_token.get_secret_value(), "CONTROL_TOKEN": password,
        "MAX_OUTPUT_TOKENS": "768",
    }
    for name, value in values.items():
        monkeypatch.setenv("COMPANION_" + name, value)
    runtime = Settings.load()
    assert runtime.max_output_tokens == 1024 and runtime._operator_config_version == 1
    deployment = Settings.load(apply_operator_config=False)
    assert deployment.max_output_tokens == 768 and deployment._operator_config_version == 0
    assert runtime.database == deployment.database == core.settings.database
    assert runtime.erasure_ledger == deployment.erasure_ledger == core.settings.erasure_ledger

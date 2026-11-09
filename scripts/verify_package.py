"""Check release archives and initialize a database using only the extracted wheel."""

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import zipfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from companion import __version__  # noqa: E402

FORBIDDEN = {
    ".local",
    ".venv",
    ".env",
    "config.toml",
    "gateway.toml",
    "data",
    "privacy",
    "backups",
    ".codex-remote-attachments",
    "__pycache__",
    ".playwright-cli",
}
MEME_SOURCE_FILES = {
    "companion_meme_sources/" + name
    for name in ("__init__.py", "fetch.py", "community.py", "review.py", "gallery.py")
}


def verify_source(archive):
    assert {f"companion_core-{__version__}/{name}" for name in MEME_SOURCE_FILES} <= (
        set(archive.getnames())
    )
    assert not any(FORBIDDEN.intersection(Path(n).parts) for n in archive.getnames())
    assert not any(part.endswith(".owner-images")
                   for name in archive.getnames() for part in Path(name).parts)
    assert not any("output/playwright/" in n for n in archive.getnames())
    assert not any(Path(n).suffix.lower() in {".node", ".dll", ".so", ".enc"}
                   for n in archive.getnames())


def verify_export_rebuild(source, temporary):
    # Git ignore rules are absent in user source exports and may differ in worktrees.
    with tempfile.TemporaryDirectory(prefix="export-package-check-", dir=temporary) as folder:
        destination = Path(folder).resolve()
        with tarfile.open(source) as archive:
            archive.extractall(destination, filter="data")
        exported = destination / f"companion_core-{__version__}"
        for name in (
            ".env",
            "config.toml",
            "gateway.toml",
            ".local/canary.txt",
            "data/canary.sqlite3",
            "privacy/canary.jsonl",
            "backups/canary.enc",
            "custom.db.owner-images/canary.raw",
        ):
            target = exported / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("SYNTHETIC_PACKAGING_CANARY_NOT_A_SECRET", encoding="utf-8")
        rebuilt = destination / "rebuilt"
        subprocess.run(
            ["uv", "build", "--sdist", "--project", str(exported), "--out-dir", str(rebuilt)],
            cwd=destination,
            check=True,
            timeout=120,
        )
        with tarfile.open(rebuilt / source.name) as archive:
            verify_source(archive)
    print("PASS: exported source rebuild excludes private inputs without Git metadata")


def verify_meme_protocol(destination, env=None):
    """Import the extracted contract without either application's runtime dependencies."""
    script = textwrap.dedent("""
        import importlib.abc
        import sys
        from pathlib import Path

        destination = Path(sys.argv[1]).resolve()
        sys.path.insert(0, str(destination))
        blocked = {"companion", "companion_onebot_bridge", "sqlite3", "sqlalchemy",
                   "httpx", "fastapi", "fastembed", "uvicorn"}

        class RuntimeDependencyGuard(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".", 1)[0] in blocked:
                    raise AssertionError("Shared protocol imported runtime dependency: " + fullname)

        sys.meta_path.insert(0, RuntimeDependencyGuard())
        import companion_meme_protocol as shared
        assert Path(shared.__file__).resolve().is_relative_to(destination)
        assert shared.caption_model_schema()["title"] == "CaptionResult"
        blocked.remove("companion_onebot_bridge")
        from companion_onebot_bridge import meme_caption, meme_library, meme_semantic
        assert meme_caption.CaptionResult is shared.CaptionResult
        assert meme_caption.sample_frames is shared.sample_frames
        assert meme_semantic.caption_text is shared.caption_text
        assert meme_library.PreparedImage is shared.PreparedImage
        assert shared.validate_bytes(b"invalid image") is None
        assert "companion" not in sys.modules and "sqlite3" not in sys.modules
    """)
    subprocess.run([sys.executable, "-I", "-c", script, str(destination)], cwd=destination,
                   env=env, check=True, timeout=30)


def verify_meme_sources(destination, env=None):
    """Load acquisition code from the artifact, without either application's authority."""
    script = textwrap.dedent("""
        import importlib.abc
        import sys
        from pathlib import Path

        destination = Path(sys.argv[1]).resolve()
        sys.path.insert(0, str(destination))
        blocked = {"companion", "companion_onebot_bridge", "sqlite3", "sqlalchemy",
                   "fastapi", "fastembed", "uvicorn", "httpx", "pydantic", "PIL",
                   "companion_meme_protocol"}

        class RuntimeDependencyGuard(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".", 1)[0] in blocked:
                    raise AssertionError("Shared sources imported runtime dependency: " + fullname)

        sys.meta_path.insert(0, RuntimeDependencyGuard())
        import companion_meme_sources as sources
        assert not blocked.intersection(sys.modules)
        blocked.difference_update({"httpx", "pydantic", "PIL", "companion_meme_protocol"})
        from companion_meme_sources import community, fetch, gallery, review
        assert fetch.FetchedResource is sources.FetchedResource
        assert community.RepositoryImage is sources.RepositoryImage
        assert review.Metadata is sources.Metadata
        assert review.ReviewApproval is sources.ReviewApproval
        assert callable(fetch.PublicFetcher) and callable(community.CommunityPacks)
        assert callable(gallery.AigengtuGallery)
        assert review.TrustedReviewer([], require_semantics=False).rules == ()
        for name, module in tuple(sys.modules.items()):
            if name.split(".", 1)[0] in {"companion_meme_sources", "companion_meme_protocol"}:
                assert Path(module.__file__).resolve().is_relative_to(destination), name
        assert not blocked.intersection(sys.modules)
    """)
    subprocess.run([sys.executable, "-I", "-B", "-c", script, str(destination)], cwd=destination,
                   env=env, check=True, timeout=30)


def verify_postgres(destination, environment):
    """Run the extracted wheel's native migration in an independent process."""
    configuration = os.environ.get("COMPANION_POSTGRES_TEST_CONFIG")
    if not configuration:
        raise RuntimeError("POSTGRES_PACKAGE_VERIFICATION_REQUIRES_TEST_CONFIG")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    value = json.loads(Path(configuration).read_text("utf8"))
    connection = {key: value[key] for key in ["host", "port", "dbname", "user", "password"]}
    namespace = "package_" + uuid4().hex
    script = textwrap.dedent("""
        import json,sys
        from pathlib import Path
        sys.path.insert(0,sys.argv[1])
        from psycopg.conninfo import make_conninfo
        from pydantic import SecretStr
        import companion.postgres as native
        assert Path(native.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
        value=json.loads(Path(sys.argv[2]).read_text('utf8'))
        connection={key:value[key] for key in ['host','port','dbname','user','password']}
        options='-c search_path='+sys.argv[3]+',pg_catalog'
        info=SecretStr(make_conninfo(**connection,options=options))
        native.migrate(info)
        with native.Database(info) as database, database.session() as session:
            assert session.one('SELECT version_num FROM alembic_version')=={
                'version_num':'pg0002_relationship_standing'}
            assert session.one("SELECT count(*) AS n FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_type='BASE TABLE'")=={'n':104}
        print('PASS: extracted wheel initializes native PostgreSQL migrations')
    """)
    with psycopg.connect(**connection, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
    try:
        init_environment = {**environment,
            "COMPANION_DATABASE": make_conninfo(
                **connection, options="-c search_path=" + namespace + ",pg_catalog"),
            "COMPANION_STATE_DIRECTORY": str(destination / "check-state"),
        }
        subprocess.run([sys.executable, "-B", "-m", "companion", "init"],
                       cwd=destination, env=init_environment, check=True, timeout=60)
        subprocess.run([sys.executable, "-I", "-B", "-c", script, str(destination),
                        str(Path(configuration).resolve()), namespace],
                       cwd=destination, check=True, timeout=60)
    finally:
        with psycopg.connect(**connection, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(namespace)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    distribution = parser.parse_args().dist_dir.resolve()
    wheel = distribution / f"companion_core-{__version__}-py3-none-any.whl"
    source = distribution / f"companion_core-{__version__}.tar.gz"
    temporary = ROOT / ".local"
    temporary.mkdir(exist_ok=True)
    with tarfile.open(source) as archive:
        verify_source(archive)
    verify_export_rebuild(source, temporary)
    with zipfile.ZipFile(wheel) as archive:
        assert MEME_SOURCE_FILES <= set(archive.namelist())
        assert not any(FORBIDDEN.intersection(Path(n).parts) for n in archive.namelist())
        assert not any(part.endswith(".owner-images")
                       for name in archive.namelist() for part in Path(name).parts)
        assert not any(Path(n).suffix.lower() in {".node", ".dll", ".so", ".enc"}
                       for n in archive.namelist())
        assert {
            "companion/media_web/index.html",
            "companion/media_web/app.js",
            "companion/media_web/style.css",
            "companion/console_static/index.html",
            "companion/console_static/app.js",
            "companion/console_static/app.css",
            "source_reader/__main__.py",
            "source_reader/network.py",
            "source_reader/THIRD_PARTY_NOTICES.txt",
            "source_reader/accounts.py",
            "source_reader/heybox_source.py",
            "source_reader/bili_tools.py",
            "source_reader/bili_media.py",
            "source_reader/video_extract.py",
            "companion/bili_video_protocol.py",
            "companion/video_media_contract.py",
            "companion_onebot_bridge/__main__.py",
            "companion_onebot_bridge/spool.py",
            "companion_onebot_bridge/voice_media.py",
            "companion_onebot_bridge/account_actions.py",
            "companion_meme_protocol/__init__.py",
            "companion/voice_protocol.py",
            "companion_voice_node/__init__.py",
            "companion_voice_node/providers.py",
            "companion_voice_node/worker.py",
            "companion_voice_node/state.py",
            "companion_voice_node/supervisor.py",
            "companion_delivery_protocol/__init__.py",
            "companion/telemetry.py",
            "companion/postgres.py",
            "companion/legacy_import.py",
            "companion/migrations/postgresql/env.py",
            "companion/migrations/postgresql/schema.sql",
            "companion/migrations/postgresql/constraints.sql",
            "companion/migrations/postgresql/versions/pg0001_core.py",
            "companion/migrations/postgresql/versions/pg0002_relationship_standing.py",
        } <= set(archive.namelist())
        with tempfile.TemporaryDirectory(prefix="package-check-", dir=temporary) as folder:
            destination = Path(folder).resolve()
            assert destination.is_relative_to(temporary.resolve())
            archive.extractall(destination)
            env = {k: v for k, v in os.environ.items() if not k.startswith("COMPANION_")}
            env.update(
                COMPANION_OWNER_QQ_ID="10001",
                COMPANION_BOT_QQ_ID="10002",
                COMPANION_BRIDGE_TOKEN="package-verification-token-32-chars",
                COMPANION_ERASURE_LEDGER=str(destination / "erasures.jsonl"),
                PYTHONDONTWRITEBYTECODE="1",
                PYTHONPATH=str(destination),
            )
            verify_meme_protocol(destination, env)
            verify_meme_sources(destination, env)
            verify_postgres(destination, env)
            subprocess.run(
                [sys.executable, "-m", "source_reader", "--help"],
                cwd=destination,
                env=env,
                check=True,
                timeout=30,
            )
            subprocess.run(
                [sys.executable, "-m", "companion_voice_node.worker", "--help"],
                cwd=destination, env=env, check=True, timeout=30,
            )
    print("PASS: archives exclude private inputs; extracted wheel initializes its migrations")


if __name__ == "__main__":
    main()

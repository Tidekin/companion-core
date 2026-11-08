"""Owned native test databases; template copies are isolated per pytest worker.

https://www.postgresql.org/docs/16/sql-createdatabase.html
Only the explicitly configured ephemeral test cluster is used. No live service DSN
or fallback database is consulted, and only this context's generated database is dropped.
"""

import json
import os
import re
from contextlib import contextmanager, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import SecretStr

from companion.backup import _client, _run
from companion.postgres import Database, migrate


def connection_parameters():
    path = os.environ.get("COMPANION_POSTGRES_TEST_CONFIG")
    if not path:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("native PostgreSQL test service is mandatory in CI")
        pytest.skip("isolated PostgreSQL integration service is not configured")
    value = json.loads(Path(path).read_text("utf8"))
    return {**{key: value[key] for key in ("host", "port", "dbname", "user", "password")},
            "connect_timeout": 5}


def logical_state(store):
    """Compare all fixture table values without PostgreSQL's physical/WAL noise."""
    with nullcontext() if store.in_transaction else store.read_snapshot():
        tables = store.rows("SELECT schemaname,tablename FROM pg_tables "
                            "WHERE schemaname=current_schema() ORDER BY tablename")
        statements = [sql.SQL(
            "SELECT %s::text AS name,COALESCE(jsonb_agg(to_jsonb(t) "
            "ORDER BY to_jsonb(t)::text),'[]'::jsonb) AS rows FROM {} AS t"
        ).format(sql.Identifier(row['schemaname'], row['tablename'])) for row in tables]
        if not statements:
            return []
        # Parallel Append may return UNION ALL branches in any order.
        query = sql.SQL(" UNION ALL ").join(statements) + sql.SQL(" ORDER BY name")
        return store.rows(query, [row["tablename"] for row in tables])


@contextmanager
def isolated_database(*, template=None):
    connection = connection_parameters()
    name = "fixture_" + uuid4().hex
    source = conninfo_to_dict(template.get_secret_value())["dbname"] if template else "template0"
    if source != "template0" and not re.fullmatch("fixture_[a-f0-9]{32}", source):
        raise RuntimeError("POSTGRES_TEST_TEMPLATE_OWNERSHIP_REQUIRED")
    with psycopg.connect(**connection, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(source)
            )
        )
    try:
        yield SecretStr(
            make_conninfo(
                **{**connection, "dbname": name}, options="-c search_path=public,pg_catalog"
            )
        )
    finally:
        assert re.fullmatch("fixture_[a-f0-9]{32}", name)
        with psycopg.connect(**connection, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def copy_database(source, destination, scratch_directory):
    """Copy a pre-event test snapshot without running application recovery or ledger replay."""
    for info in (source, destination):
        if not re.fullmatch(
            "fixture_[a-f0-9]{32}", conninfo_to_dict(info.get_secret_value())["dbname"]
        ):
            raise ValueError("Expected an isolated fixture database")
    with psycopg.connect(destination.get_secret_value()) as db:
        if db.execute(
            "SELECT 1 FROM pg_tables WHERE schemaname=current_schema() LIMIT 1"
        ).fetchone():
            raise ValueError("Expected an empty fixture database")
    with TemporaryDirectory(dir=scratch_directory) as scratch:
        dump = str(Path(scratch) / "pre-event.dump")
        with TemporaryDirectory(dir=scratch) as client, _client(source, client) as (_, env):
            _run(
                "pg_dump", ["--format=custom", "--no-owner", "--no-privileges", "--file", dump], env
            )
        with TemporaryDirectory(dir=scratch) as client, _client(destination, client) as (_, env):
            _run("pg_restore", ["--clean", "--if-exists", "--no-owner", "--no-privileges",
                                "--single-transaction", dump], env)


@contextmanager
def isolated_native():
    configuration = os.environ.get("COMPANION_POSTGRES_TEST_CONFIG")
    if not configuration:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("native PostgreSQL test service is mandatory in CI")
        pytest.skip("isolated PostgreSQL integration service is not configured")
    value = json.loads(Path(configuration).read_text("utf8"))
    connection = {key: value[key] for key in ["host", "port", "dbname", "user", "password"]}
    namespace = "pytest_" + uuid4().hex
    with psycopg.connect(**connection, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
    info = SecretStr(make_conninfo(**connection, options=f"-c search_path={namespace},pg_catalog"))
    try:
        migrate(info)
        with Database(info, max_connections=4) as database:
            yield database, info
    finally:
        assert re.fullmatch(r"pytest_[a-f0-9]{32}", namespace)
        with psycopg.connect(**connection, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(namespace)))

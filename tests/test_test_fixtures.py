"""Shared setup must preserve the isolation and schema exercised by runtime tests."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from tests.conftest import ingest
from tests.support.historical_schema import assert_schema_metadata


@pytest.mark.parametrize("attempt", [1, 2])
def test_core_fixture_keeps_runtime_data_out_of_the_template(core, migrated_database, attempt):
    # Both invocations start empty even though the prior invocation wrote an event.
    assert core.store.rows("SELECT event_id FROM events") == []
    assert core.store.schema_capabilities()
    assert core.store.one("PRAGMA foreign_keys")["foreign_keys"] == 1
    assert core.store.one("PRAGMA synchronous")["synchronous"] == 2
    assert core.store.one("PRAGMA secure_delete")["secure_delete"] == 1
    ingest(core, attempt, text="合成夹具隔离检查")
    assert len(core.store.rows("SELECT event_id FROM events")) == 1

    with closing(sqlite3.connect(migrated_database)) as template:
        # Only migration-owned identity/OFF markers and FTS internal rows may exist;
        # identities, privacy epochs, leases, erasure and budget state stay empty.
        assert_schema_metadata(template)
        tables = template.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for (name,) in tables:
            if (name not in {"alembic_version", "runtime_meta"}
                and not name.startswith("memories_fts_")):
                assert template.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] == 0


def test_reusable_setup_never_imports_a_business_test_suite():
    import ast
    import re
    from itertools import pairwise

    root = Path(__file__).resolve().parents[1]
    bad = []
    # Workers also load Python through -c scripts and -m module arguments.
    child_import = re.compile(
        r"(?:^|;)\s*(?:from\s+tests\.test_\w+\s+import\b|"
        r"from\s+tests\s+import\s+test_\w+|import\s+tests\.test_\w+)", re.MULTILINE)
    for directory in ("tests", "scripts", "benchmarks"):
        for path in (root / directory).rglob("*.py"):
            text = path.read_text(encoding="utf-8-sig")
            if "tests.test_" not in text and not re.search(r"from\s+tests\s+import\b", text):
                continue
            tree = ast.parse(text)
            docstrings = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    names = [f"{node.module}.{alias.name}" for alias in node.names]
                elif isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if id(node) not in docstrings and child_import.search(node.value):
                        bad.append(f"{path.relative_to(root)}:{node.lineno}")
                    continue
                elif isinstance(node, (ast.Call, ast.List, ast.Tuple)):
                    args = node.args if isinstance(node, ast.Call) else node.elts
                    names = [module.value for option, module in pairwise(args)
                             if isinstance(option, ast.Constant) and option.value == "-m"
                             and isinstance(module, ast.Constant) and isinstance(module.value, str)]
                else:
                    continue
                if any(name.startswith("tests.test_") for name in names):
                    bad.append(f"{path.relative_to(root)}:{node.lineno}")
    assert bad == []

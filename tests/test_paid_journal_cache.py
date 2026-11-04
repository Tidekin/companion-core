"""Chain bytes are reusable; a whole view only while journal bytes and the DB are unchanged."""

import json
import marshal
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion import budget, cumulative, paid_budget
from companion.contracts import CoreError
from companion.model import ModelResponse
from tests.owner_model_fixtures import authorized_job
from tests.test_paid_model_gate import approved_live


@pytest.fixture(autouse=True)
def clear_cache():
    cache = getattr(paid_budget, "_validated_bytes", None)
    if cache:
        cache.cache_clear()
    paid_budget.forget_views()
    yield
    if cache:
        cache.cache_clear()
    paid_budget.forget_views()


def settled(core, tmp_path):
    approval = approved_live(core, tmp_path, window_calls=10, daily_calls=10)
    request = budget.reserve(core.store, core.settings, authorized_job(core), "chat",
                             [{"role": "user", "content": "synthetic"}], core.clock())
    budget.settle(core.store, core.settings, request, ModelResponse("ok", 80, 3))
    return approval, request


def test_warm_chain_reads_complete_bytes_and_skips_only_chain_hashes(core, tmp_path, monkeypatch):
    approval, request = settled(core, tmp_path)
    path = Path(approval.journal_path)
    reads, hashes, queries = [], [], []
    original_read, original_sha = Path.read_bytes, paid_budget.sha

    def read_bytes(value):
        raw = original_read(value)
        if value == path:
            reads.append(len(raw))
        return raw

    def sha(value):
        if isinstance(value, dict) and "previous" in value and "kind" in value:
            hashes.append(1)
        return original_sha(value)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(paid_budget, "sha", sha)
    cache = getattr(paid_budget, "_validated_bytes", None)
    if cache:
        cache.cache_clear()
    core.store.db.set_trace_callback(queries.append)
    try:
        with core.store.transaction():
            first = paid_budget.journal(core.store, approval)
            cold = len(hashes)
            first["model_latest"][request]["minimum_micro_usd"] = 0
            before = sum(query.startswith("SELECT") for query in queries)
            second = paid_budget.journal(core.store, approval)
            after = sum(query.startswith("SELECT") for query in queries) - before
    finally:
        core.store.db.set_trace_callback(None)
    assert reads == [path.stat().st_size] * 2
    assert cold == len(path.read_text("utf-8").splitlines()) and len(hashes) == cold
    # Unchanged bytes and DB reuse the validated view instead of folding again.
    assert before > after
    assert second["model_latest"][request]["minimum_micro_usd"] > 0
    assert first["head_sha256"] == second["head_sha256"]


@pytest.mark.parametrize("damage", ["same_size", "truncate", "json", "missing"])
def test_warm_chain_rejects_changed_complete_bytes(core, tmp_path, damage):
    approval, _ = settled(core, tmp_path)
    path = Path(approval.journal_path)
    with core.store.transaction():
        paid_budget.journal(core.store, approval)
    raw, stamp = path.read_bytes(), path.stat()
    if damage == "same_size":
        changed = raw.replace(b'"previous":"0', b'"previous":"9', 1)
        assert len(changed) == len(raw) and changed != raw
    elif damage == "truncate":
        changed = raw.splitlines(keepends=True)[0]
    elif damage == "json":
        changed = raw + b"{\n"
    else:
        path.unlink()
        changed = None
    if changed is not None:
        path.write_bytes(changed)
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        with core.store.transaction():
            paid_budget.journal(core.store, approval)
    assert paid_budget._validated_bytes.cache_info().currsize == 0


@pytest.mark.parametrize("damage", ["amount", "state", "measurement", "identity"])
def test_warm_bytes_never_cache_current_database(core, tmp_path, damage):
    approval, request = settled(core, tmp_path)
    with core.store.transaction():
        paid_budget.journal(core.store, approval)
    if damage == "amount":
        core.store.execute("UPDATE model_usage SET billed_micro_usd=0 WHERE request_id=?",
                           (request,))
    elif damage == "state":
        core.store.execute("UPDATE model_usage SET state='UNKNOWN' WHERE request_id=?", (request,))
    elif damage == "measurement":
        core.store.execute("UPDATE model_admissions SET measurement_json=? WHERE request_id=?",
                           (json.dumps({"version": 999}), request))
    else:
        core.store.execute("UPDATE runtime_meta SET value=? WHERE key='model_budget_journal_id'",
                           ("budget_" + "f" * 32,))
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        with core.store.transaction():
            paid_budget.journal(core.store, approval)
    assert paid_budget._validated_bytes.cache_info().currsize == 0


@pytest.mark.parametrize("tail", ["json", "hash", "depth"])
def test_bad_tail_preserves_earlier_fold_error_and_durable_stop(core, tmp_path, monkeypatch, tail):
    approval, _ = settled(core, tmp_path)
    path = Path(approval.journal_path)
    with core.store.transaction():
        view = paid_budget.journal(core.store, approval)
    raw = path.read_bytes()
    if tail == "json":
        path.write_bytes(raw + b"{\n")
    elif tail == "depth":
        depth = 20000
        fragment = b"[" * depth + b"]" * depth
        with pytest.raises(RecursionError):
            json.loads(fragment)
        path.write_bytes(raw + fragment + b"\n")
    else:
        record = json.loads(raw.splitlines()[-1])
        record["sha256"] = "f" * 64
        path.write_text("\n".join([*raw.decode().splitlines()[:-1], json.dumps(record)]) + "\n",
                        "utf-8")
    calls = []

    def fold(state, row):
        calls.append(row["kind"])
        identity = core.store.one("SELECT value FROM runtime_meta "
                                  "WHERE key='model_budget_journal_id'")["value"]
        core.store.retain_budget_stop(identity, view["head_sha256"])
        raise CoreError("SYNTHETIC_PREFIX_STOP", 409)

    monkeypatch.setattr(cumulative, "fold", fold)
    with pytest.raises(CoreError, match="SYNTHETIC_PREFIX_STOP"):
        with core.store.transaction():
            paid_budget.journal(core.store, approval)
    assert len(calls) == 1
    assert core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                          ("model_budget_stop:" + view["head_sha256"],))


def test_over_limit_read_bypasses_and_clears_cache(core, tmp_path, monkeypatch):
    approval, _ = settled(core, tmp_path)
    with core.store.transaction():
        expected = paid_budget.journal(core.store, approval)
    monkeypatch.setattr(paid_budget, "CACHE_MAX_BYTES", 1)
    with core.store.transaction():
        assert paid_budget.journal(core.store, approval) == expected
    assert paid_budget._validated_bytes.cache_info().currsize == 0


def test_large_source_reuses_only_a_small_verified_view(core, tmp_path, monkeypatch):
    approval, request = settled(core, tmp_path)
    path = Path(approval.journal_path)
    # Whitespace preserves each parsed record and chain hash while crossing the
    # codec's source-byte limit with the original accounting state.
    raw = path.read_bytes().rstrip(b"\n") + b" " * (paid_budget.CACHE_MAX_BYTES + 1) + b"\n"
    path.write_bytes(raw)
    paid_budget.forget_views(core.store)
    calls = []
    original = paid_budget._journal

    def replay(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(paid_budget, "_journal", replay)
    with core.store.transaction():
        first = paid_budget.journal(core.store, approval)
        assert len(marshal.dumps(first)) < paid_budget.CACHE_MAX_BYTES < path.stat().st_size
        first["model_latest"][request]["minimum_micro_usd"] = 0
        second = paid_budget.journal(core.store, approval)
        assert second["model_latest"][request]["minimum_micro_usd"] > 0
        assert len(calls) == 1
        assert paid_budget._validated_bytes.cache_info().currsize == 0

    # Every subsequent use reads the original bytes again.
    path.write_bytes(path.read_bytes() + b"corrupt")
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        paid_budget.journal(core.store, approval)


def test_oversized_folded_result_is_not_retained(core, tmp_path, monkeypatch):
    approval, _ = settled(core, tmp_path)
    with core.store.transaction():
        expected = paid_budget.journal(core.store, approval)
    paid_budget.forget_views(core.store)
    monkeypatch.setattr(paid_budget, "CACHE_MAX_BYTES", 1)
    with core.store.transaction():
        assert paid_budget.journal(core.store, approval) == expected
        assert core.store._journal_view is None
        assert not paid_budget._SHARED


@pytest.mark.parametrize("separator", ["\r\n", "\r", "\u2028"])
def test_cached_codec_keeps_existing_newline_and_duplicate_key_rules(core, tmp_path, separator):
    approval, _ = settled(core, tmp_path)
    path = Path(approval.journal_path)
    with core.store.transaction():
        expected = paid_budget.journal(core.store, approval)
    lines = path.read_text("utf-8").splitlines()
    lines[0] = lines[0].replace('"kind":"header"', '"kind":"header","kind":"header"')
    path.write_bytes((separator.join(lines) + separator).encode())
    with core.store.transaction():
        assert paid_budget.journal(core.store, approval) == expected
        assert paid_budget.journal(core.store, approval) == expected


def test_appended_record_replaces_the_one_cached_chain(core, tmp_path):
    approval, _ = settled(core, tmp_path)
    with core.store.transaction():
        before = paid_budget.journal(core.store, approval)
    request = budget.reserve(core.store, core.settings, authorized_job(core), "chat",
                             [{"role": "user", "content": "another"}], core.clock())
    budget.settle(core.store, core.settings, request, ModelResponse("ok", 90, 4))
    with core.store.transaction():
        after = paid_budget.journal(core.store, approval)
        assert paid_budget.journal(core.store, approval) == after
    assert before["head_sha256"] != after["head_sha256"]
    assert len(after["model_latest"]) == len(before["model_latest"]) + 1
    assert paid_budget._validated_bytes.cache_info().currsize == 1


def test_warm_read_failure_clears_cache_and_cannot_return_old_view(core, tmp_path, monkeypatch):
    approval, _ = settled(core, tmp_path)
    with core.store.transaction():
        paid_budget.journal(core.store, approval)
    original = Path.read_bytes

    def read_bytes(path):
        if path == Path(approval.journal_path):
            raise OSError("synthetic independent file failure")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        with core.store.transaction():
            paid_budget.journal(core.store, approval)
    assert paid_budget._validated_bytes.cache_info().currsize == 0


def test_valid_warm_chain_keeps_fold_failure_and_clears_cache(core, tmp_path, monkeypatch):
    approval, _ = settled(core, tmp_path)
    with core.store.transaction():
        expected = paid_budget.journal(core.store, approval)

    def fold(state, row):
        raise CoreError("SYNTHETIC_CURRENT_FOLD_FAILURE", 409)

    monkeypatch.setattr(cumulative, "fold", fold)
    with core.store.transaction():
        # Nothing changed: the validated view is reused without folding again.
        assert paid_budget.journal(core.store, approval) == expected
    # Any DB write invalidates it; the next read folds and keeps the failure.
    core.store.execute("INSERT INTO runtime_meta VALUES ('synthetic_db_change','1')")
    with pytest.raises(CoreError, match="SYNTHETIC_CURRENT_FOLD_FAILURE"):
        with core.store.transaction():
            paid_budget.journal(core.store, approval)
    assert paid_budget._validated_bytes.cache_info().currsize == 0


def test_warm_chain_does_not_cache_settlement_permission_or_lower_charges(core, tmp_path):
    approval, request = settled(core, tmp_path)
    before = core.store.one("SELECT billed_micro_usd FROM model_usage WHERE request_id=?",
                            (request,))["billed_micro_usd"]
    with core.store.transaction():
        admission = core.store.one("SELECT * FROM model_admissions WHERE request_id=?", (request,))
        budget._persist_accounting(core.store, admission, before, "UNKNOWN", True)
        view = paid_budget.journal(core.store, approval, allow_settlement=True)
        assert view["blocked_request_ids"] == [request]
        with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
            paid_budget.journal(core.store, approval)
    assert paid_budget._validated_bytes.cache_info().currsize == 0
    assert core.store.one("SELECT billed_micro_usd,state FROM model_usage WHERE request_id=?",
                          (request,)) == {"billed_micro_usd": before, "state": "UNKNOWN"}


@pytest.mark.parametrize("failure", ["path", "identity"])
def test_preflight_failure_clears_cache_without_reclassifying_error(
    core, tmp_path, monkeypatch, failure,
):
    approval, _ = settled(core, tmp_path)
    with core.store.transaction():
        paid_budget.journal(core.store, approval)
    if failure == "path":
        approval = SimpleNamespace(journal_path=object())
        expected = TypeError
    else:
        def one(*args, **kwargs):
            raise OSError("synthetic identity read failure")

        monkeypatch.setattr(core.store, "one", one)
        expected = OSError
    with pytest.raises(expected):
        with core.store.transaction():
            paid_budget.journal(core.store, approval)
    assert paid_budget._validated_bytes.cache_info().currsize == 0


def test_payload_decode_failure_uses_original_line_decoder(core, tmp_path, monkeypatch):
    approval, _ = settled(core, tmp_path)
    with core.store.transaction():
        expected = paid_budget.journal(core.store, approval)
    original, failures = json.loads, []

    def loads(value, *args, **kwargs):
        if isinstance(value, bytes) and value.startswith(b"["):
            failures.append(1)
            raise RecursionError("synthetic aggregate decoder limit")
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", loads)
    paid_budget.forget_views(core.store)  # exercise the cold decode path
    with core.store.transaction():
        assert paid_budget.journal(core.store, approval) == expected
    assert failures == [1]
    assert paid_budget._validated_bytes.cache_info().currsize == 0


@pytest.mark.parametrize("large", [False, True])
def test_a_new_connection_reuses_the_shared_view_until_any_commit(core, tmp_path, large):
    from companion.store import Store

    approval, request = settled(core, tmp_path)
    if large:
        path = Path(approval.journal_path)
        path.write_bytes(b" " * (paid_budget.CACHE_MAX_BYTES + 1) + path.read_bytes())
        paid_budget.forget_views(core.store)

    with core.store.transaction():
        expected = paid_budget.journal(core.store, approval)
    other = Store(core.settings.database)
    try:
        queries = []
        other.db.set_trace_callback(queries.append)
        with other.transaction():
            assert paid_budget.journal(other, approval) == expected
        other.db.set_trace_callback(None)
        # Reused: no fold or projection query ran on the fresh connection.
        assert not any("model_admissions a" in query for query in queries)
        # A commit from another connection invalidates the shared view.
        core.store.execute("UPDATE model_usage SET billed_micro_usd=0 WHERE request_id=?",
                           (request,))
        with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
            with other.transaction():
                paid_budget.journal(other, approval)
    finally:
        other.close()


def test_uncommitted_writes_never_fill_or_use_the_shared_view(core, tmp_path):
    approval, _ = settled(core, tmp_path)
    paid_budget.forget_views()
    with core.store.transaction():
        core.store.execute("INSERT INTO runtime_meta VALUES ('synthetic_pending','1')")
        paid_budget.journal(core.store, approval)
        assert paid_budget._SHARED == {}

"""Chain bytes are reusable; a whole view only while journal bytes and the DB are unchanged."""

import json
import marshal
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import sleep
from types import SimpleNamespace

import pytest
from psycopg import sql

from companion import budget, cumulative, paid_budget
from companion.contracts import CoreError
from companion.model import ModelResponse
from companion.store import Store, encode
from tests.owner_model_fixtures import authorized_job
from tests.support.paid_model import approved_live


@pytest.fixture(autouse=True)
def clear_cache():
    paid_budget._clear_codec()
    paid_budget.forget_views()
    yield
    paid_budget._clear_codec()
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
    paid_budget._clear_codec()
    original_one = core.store.one
    original_rows = core.store.rows

    def observed(method):
        def read(query, params=()):
            queries.append(str(query))
            return method(query, params)
        return read

    monkeypatch.setattr(core.store, "one", observed(original_one))
    monkeypatch.setattr(core.store, "rows", observed(original_rows))
    with core.store.read_snapshot():
        first = paid_budget.journal(core.store, approval)
        cold = len(hashes)
        first["model_latest"][request]["minimum_micro_usd"] = 0
        before = len(queries)
        second = paid_budget.journal(core.store, approval)
        after = len(queries) - before
    assert reads == [path.stat().st_size] * 2
    assert cold == len(path.read_text("utf-8").splitlines()) and len(hashes) == cold
    # Unchanged bytes and DB reuse the validated view instead of folding again.
    assert before > after
    assert second["model_latest"][request]["minimum_micro_usd"] > 0
    assert first["head_sha256"] == second["head_sha256"]


@pytest.mark.parametrize("bounded_prefix", [False, True])
@pytest.mark.parametrize("damage", ["same_size", "truncate", "json", "missing"])
def test_warm_chain_rejects_changed_complete_bytes(
    core, tmp_path, monkeypatch, damage, bounded_prefix,
):
    approval, _ = settled(core, tmp_path)
    path = Path(approval.journal_path)
    if bounded_prefix:
        monkeypatch.setattr(paid_budget, "CACHE_MAX_BYTES",
                            len(path.read_bytes().splitlines(keepends=True)[0]))
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
        core.store.execute("UPDATE model_usage SET billed_micro_usd=0 WHERE request_id=%s",
                           (request,))
    elif damage == "state":
        core.store.execute("UPDATE model_usage SET state='UNKNOWN' WHERE request_id=%s", (request,))
    elif damage == "measurement":
        core.store.execute("UPDATE model_admissions SET measurement_json=%s WHERE request_id=%s",
                           (json.dumps({"version": 999}), request))
    else:
        core.store.execute("UPDATE runtime_meta SET value=%s WHERE key='model_budget_journal_id'",
                           ("budget_" + "f" * 32,))
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        with core.store.transaction():
            paid_budget.journal(core.store, approval)
    assert paid_budget._validated_bytes.cache_info().currsize == 0


@pytest.mark.parametrize("bounded_prefix", [False, True])
@pytest.mark.parametrize("tail", ["json", "hash", "depth"])
def test_bad_tail_preserves_earlier_fold_error_and_durable_stop(
    core, tmp_path, monkeypatch, tail, bounded_prefix,
):
    approval, _ = settled(core, tmp_path)
    path = Path(approval.journal_path)
    with core.store.transaction():
        view = paid_budget.journal(core.store, approval)
    raw = path.read_bytes()
    if bounded_prefix:
        monkeypatch.setattr(paid_budget, "CACHE_MAX_BYTES",
                            len(raw.splitlines(keepends=True)[0]))
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
    assert core.store.one("SELECT value FROM runtime_meta WHERE key=%s",
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
    # Reuse is guaranteed in one immutable snapshot. READ COMMITTED may see
    # unrelated concurrent transactions and conservatively rebuild its view.
    with core.store.read_snapshot():
        first = paid_budget.journal(core.store, approval)
        assert len(marshal.dumps(first)) < paid_budget.CACHE_MAX_BYTES < path.stat().st_size
        first["model_latest"][request]["minimum_micro_usd"] = 0
        second = paid_budget.journal(core.store, approval)
        assert second["model_latest"][request]["minimum_micro_usd"] > 0
        assert len(calls) == 1
        assert paid_budget._validated_bytes.cache_info().currsize == 1

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


def test_bounded_chain_prefix_survives_appends_without_reusing_mutable_records(
    core, tmp_path, monkeypatch,
):
    approval, _ = settled(core, tmp_path)
    path = Path(approval.journal_path)
    raw = path.read_bytes()
    prefix = b"".join(raw.splitlines(keepends=True)[:-1])
    monkeypatch.setattr(paid_budget, "CACHE_MAX_BYTES", len(prefix))
    records, checked = paid_budget._journal_records(raw)
    assert checked
    records[0]["kind"] = "mutated caller copy"
    before = paid_budget._validated_bytes.cache_info()
    request = budget.reserve(core.store, core.settings, authorized_job(core), "chat",
                             [{"role": "user", "content": "another"}], core.clock())
    budget.settle(core.store, core.settings, request, ModelResponse("ok", 90, 4))
    with core.store.transaction():
        view = paid_budget.journal(core.store, approval)
    assert view["model_latest"][request]["state"] == "SETTLED"
    assert path.read_bytes().startswith(prefix)
    after = paid_budget._validated_bytes.cache_info()
    assert after.misses == before.misses and after.hits > before.hits
    decoded, checked = paid_budget._journal_records(path.read_bytes())
    assert checked and decoded[0]["kind"] == "header"


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

    def fold(state, row):
        raise CoreError("SYNTHETIC_CURRENT_FOLD_FAILURE", 409)

    with core.store.read_snapshot():
        expected = paid_budget.journal(core.store, approval)
        monkeypatch.setattr(cumulative, "fold", fold)
        assert paid_budget.journal(core.store, approval) == expected
    # Any DB write invalidates it; the next read folds and keeps the failure.
    core.store.execute("INSERT INTO runtime_meta VALUES ('synthetic_db_change','1')")
    with pytest.raises(CoreError, match="SYNTHETIC_CURRENT_FOLD_FAILURE"):
        with core.store.transaction():
            paid_budget.journal(core.store, approval)
    assert paid_budget._validated_bytes.cache_info().currsize == 0


def test_warm_chain_does_not_cache_settlement_permission_or_lower_charges(core, tmp_path):
    approval, request = settled(core, tmp_path)
    before = core.store.one("SELECT billed_micro_usd FROM model_usage WHERE request_id=%s",
                            (request,))["billed_micro_usd"]
    with core.store.transaction():
        admission = core.store.one("SELECT * FROM model_admissions WHERE request_id=%s", (request,))
        budget._persist_accounting(core.store, admission, before, "UNKNOWN", True)
        view = paid_budget.journal(core.store, approval, allow_settlement=True)
        assert view["blocked_request_ids"] == [request]
        with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
            paid_budget.journal(core.store, approval)
    assert paid_budget._validated_bytes.cache_info().currsize == 0
    assert core.store.one("SELECT billed_micro_usd,state FROM model_usage WHERE request_id=%s",
                          (request,)) == {"billed_micro_usd": before, "state": "UNKNOWN"}


@pytest.mark.parametrize("failure", ["path", "identity"])
def test_preflight_failure_clears_cache_without_reclassifying_error(
    core, tmp_path, monkeypatch, failure,
):
    approval, _ = settled(core, tmp_path)
    with core.store.read_snapshot():
        paid_budget.journal(core.store, approval)
    # A new native snapshot must read the database identity again. The previous
    # immutable scope may reuse the identity it already read with its summary.
    with core.store.read_snapshot():
        if failure == "path":
            approval = SimpleNamespace(journal_path=object())
            expected = TypeError
        else:
            def one(*args, **kwargs):
                raise OSError("synthetic identity read failure")

            monkeypatch.setattr(core.store, "one", one)
            expected = OSError
        with pytest.raises(expected):
            paid_budget.journal(core.store, approval)
    assert paid_budget._validated_bytes.cache_info().currsize == 0


def test_payload_decode_failure_uses_original_line_decoder(core, tmp_path, monkeypatch):
    approval, _ = settled(core, tmp_path)
    with core.store.transaction():
        expected = paid_budget.journal(core.store, approval)
    original, failures = marshal.loads, []

    def loads(value, *args, **kwargs):
        if not failures:
            failures.append(1)
            raise RecursionError("synthetic aggregate decoder limit")
        return original(value, *args, **kwargs)

    monkeypatch.setattr(marshal, "loads", loads)
    paid_budget.forget_views(core.store)  # exercise the cold decode path
    with core.store.transaction():
        assert paid_budget.journal(core.store, approval) == expected
    assert failures == [1]
    assert paid_budget._validated_bytes.cache_info().currsize == 0


@pytest.mark.parametrize("large", [False, True])
def test_a_new_connection_reuses_the_shared_view_until_any_commit(core, tmp_path, large,
                                                                 monkeypatch):
    approval, request = settled(core, tmp_path)
    if large:
        path = Path(approval.journal_path)
        path.write_bytes(b" " * (paid_budget.CACHE_MAX_BYTES + 1) + path.read_bytes())
        paid_budget.forget_views(core.store)

    other = Store(core.store.database, core.settings.state_directory)
    try:
        paid_budget.forget_views(core.store)
        with core.store.database.session() as exporter, exporter.transaction(readonly=True):
            snapshot = exporter.one("SELECT pg_export_snapshot() AS id")["id"]
            import_snapshot = sql.SQL("SET TRANSACTION SNAPSHOT {}").format(sql.Literal(snapshot))
            with core.store.read_snapshot(), other.read_snapshot():
                core.store.execute(import_snapshot)
                other.execute(import_snapshot)
                expected = paid_budget.journal(core.store, approval)
                with monkeypatch.context() as patch:
                    patch.setattr(paid_budget, "_journal",
                                  lambda *a, **kw: pytest.fail("identical snapshot rebuilt view"))
                    assert paid_budget.journal(other, approval) == expected
        # A commit from another connection invalidates the shared view.
        core.store.execute("UPDATE model_usage SET billed_micro_usd=0 WHERE request_id=%s",
                           (request,))
        with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
            with other.transaction():
                paid_budget.journal(other, approval)
    finally:
        other.close()


def test_commit_during_validation_does_not_cache_under_new_database(core, tmp_path, monkeypatch):
    approval, request = settled(core, tmp_path)
    paid_budget.forget_views(core.store)
    original = paid_budget._journal
    with core.store.database.session() as writer:
        def change_after_validation(*args, **kwargs):
            view = original(*args, **kwargs)
            assert writer.execute("UPDATE model_usage SET billed_micro_usd=0 WHERE request_id=%s",
                                  (request,)) == 1
            return view

        # READ COMMITTED can see the other commit before this transaction publishes its view.
        with core.store.transaction():
            with monkeypatch.context() as patch:
                patch.setattr(paid_budget, "_journal", change_after_validation)
                paid_budget.journal(core.store, approval)
            with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
                paid_budget.journal(core.store, approval)
    with core.store.read_snapshot(), pytest.raises(CoreError, match="RECONCILIATION_REQUIRED"):
        paid_budget.journal(core.store, approval)


def test_uncommitted_writes_never_fill_or_use_the_shared_view(core, tmp_path):
    approval, _ = settled(core, tmp_path)
    paid_budget.forget_views()
    with core.store.transaction():
        core.store.execute("INSERT INTO runtime_meta VALUES ('synthetic_pending','1')")
        paid_budget.journal(core.store, approval)
        assert paid_budget._SHARED == {}


def test_simultaneous_snapshots_build_one_view_and_keep_results_private(
    core, tmp_path, monkeypatch,
):
    approval, request = settled(core, tmp_path)
    paid_budget.forget_views(core.store)
    starting = threading.Barrier(3)
    calls = []
    original = paid_budget._journal

    def build(*args, **kwargs):
        calls.append(True)
        # Make overlapping cold misses observable without relying on a large fixture.
        sleep(0.05)
        return original(*args, **kwargs)

    monkeypatch.setattr(paid_budget, "_journal", build)

    def read(snapshot):
        store = Store(core.store.database, core.settings.state_directory)
        try:
            with store.read_snapshot():
                store.execute(sql.SQL("SET TRANSACTION SNAPSHOT {}").format(sql.Literal(snapshot)))
                starting.wait(timeout=10)
                return paid_budget.journal(store, approval)
        finally:
            store.close()

    with core.store.read_snapshot(), ThreadPoolExecutor(max_workers=2) as pool:
        snapshot = core.store.one("SELECT pg_export_snapshot() AS id")["id"]
        reads = [pool.submit(read, snapshot) for _ in range(2)]
        starting.wait(timeout=10)
        values = [future.result(timeout=10) for future in reads]
    assert values[0] == values[1] and len(calls) == 1
    values[0]["model_latest"][request]["minimum_micro_usd"] = 0
    assert values[1]["model_latest"][request]["minimum_micro_usd"] > 0
    # A different connection's later edit must never inherit that shared proof.
    core.store.execute("UPDATE model_usage SET billed_micro_usd=0 WHERE request_id=%s", (request,))
    other = Store(core.store.database, core.settings.state_directory)
    try:
        with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
            other.read_or_write(lambda: paid_budget.journal(other, approval))
    finally:
        other.close()


def test_database_change_repeats_full_fold_without_redecoding_unchanged_chain(
    core, tmp_path, monkeypatch,
):
    approval, _ = settled(core, tmp_path)
    with core.store.transaction():
        expected = paid_budget.journal(core.store, approval)
    decodes, folds = [], []
    original_decode, original_fold = paid_budget._decode_lines, cumulative.fold

    def decode(lines, *args):
        decodes.extend(lines)
        return original_decode(lines, *args)

    def fold(*args):
        folds.append(True)
        return original_fold(*args)

    monkeypatch.setattr(paid_budget, "_decode_lines", decode)
    monkeypatch.setattr(cumulative, "fold", fold)
    with core.store.transaction():
        core.store.execute("INSERT INTO runtime_meta VALUES ('codec_fixture_change','1')")
        assert paid_budget.journal(core.store, approval) == expected
    assert folds and not decodes


def codec_chain(count, *, padding=0):
    rows, previous = [], "0" * 64
    for index in range(count):
        row = {"kind": "synthetic", "previous": previous,
               "data": {"nested": [index, {"text": "x" * padding}]}}
        previous = row["sha256"] = paid_budget.sha(row)
        rows.append(row)
    return ("".join(encode(row) + "\n" for row in rows)).encode(), rows


@pytest.mark.parametrize("separator", [b"\n", b"\r\n", b"\r", "\u2028".encode()])
def test_codec_append_reuses_only_an_exact_lf_terminated_prefix(monkeypatch, separator):
    complete, expected = codec_chain(3)
    lines = complete.splitlines()
    prefix = separator.join(lines[:2]) + separator
    appended = separator.join(lines) + separator
    records, checked = paid_budget._journal_records(prefix)
    assert checked and records == expected[:2]
    records[0]["data"]["nested"][1]["text"] = "caller-owned mutation"
    decodes = []
    original = paid_budget._decode_lines

    def decode(lines, *args):
        decodes.extend(lines)
        return original(lines, *args)

    monkeypatch.setattr(paid_budget, "_decode_lines", decode)
    records, checked = paid_budget._journal_records(appended)
    assert checked and records == expected
    assert len(decodes) == (1 if separator.endswith(b"\n") else 3)
    # A valid truncation is different bytes, never the prior longer projection.
    records, checked = paid_budget._journal_records(prefix)
    assert checked and records == expected[:2]


def test_small_appends_do_not_reserialize_verified_chain_prefix(monkeypatch):
    raw, expected = codec_chain(128, padding=512)
    assert paid_budget._journal_records(raw) == (expected, True)
    decodes = []
    original = paid_budget._decode_lines

    def decode(lines, *args):
        decodes.extend(lines)
        return original(lines, *args)

    def unexpected(*args):
        pytest.fail("small append reserialized the full verified prefix")

    monkeypatch.setattr(paid_budget, "_decode_lines", decode)
    monkeypatch.setattr(marshal, "dumps", unexpected)
    for index in range(3):
        row = {"kind": "synthetic", "previous": expected[-1]["sha256"], "part": index}
        row["sha256"] = paid_budget.sha(row)
        expected.append(row)
        raw += (encode(row) + "\n").encode()
        assert paid_budget._journal_records(raw) == (expected, True)
    # Every tail is fully rechecked; only the unchanged prefix is reused.
    assert len(decodes) == 1 + 2 + 3


def test_decoded_chain_over_serialized_limit_is_not_retained(monkeypatch):
    row = {"kind": "synthetic", "previous": "0" * 64, "values": [0] * 1000}
    row["sha256"] = paid_budget.sha(row)
    raw = (encode(row) + "\n").encode()
    assert len(raw) < len(marshal.dumps([row]))
    monkeypatch.setattr(paid_budget, "CACHE_MAX_BYTES", len(raw))
    assert paid_budget._journal_records(raw) == ([row], True)
    assert paid_budget._validated_bytes.cache_info().currsize == 0
    assert paid_budget._previous is None


@pytest.mark.parametrize("retain_bytes", [True, False])
def test_frozen_journal_key_reuses_digest_only_for_exact_bytes(
    core, tmp_path, monkeypatch, retain_bytes,
):
    approval, _ = settled(core, tmp_path)
    path = Path(approval.journal_path)
    raw = path.read_bytes()
    monkeypatch.setattr(paid_budget, "KEY_MAX_BYTES", len(raw) if retain_bytes else len(raw) - 1)
    hashes, original = [], paid_budget.hashlib.sha256

    def sha256(value):
        if value == raw:
            hashes.append(True)
        return original(value)

    monkeypatch.setattr(paid_budget.hashlib, "sha256", sha256)
    with core.store.read_snapshot():
        first = paid_budget._view_key(core.store, path, raw)
        assert paid_budget._view_key(core.store, path, memoryview(raw).tobytes()) == first
        assert len(hashes) == (1 if retain_bytes else 2)
        identity = core.store._view_key_memo[0][-1]
        assert isinstance(identity, bytes) is retain_bytes
    with core.store.transaction():
        before_write = paid_budget._view_key(core.store, path, raw)
        core.store.execute("INSERT INTO runtime_meta VALUES ('digest_scope_write','1')")
        changed = paid_budget._view_key(core.store, path, raw)
        assert changed != before_write
        assert len(hashes) == (3 if retain_bytes else 4)
        tampered = raw.replace(b'"previous":"0', b'"previous":"9', 1)
        assert len(tampered) == len(raw) and tampered != raw
        assert paid_budget._view_key(core.store, path, tampered)[1] != changed[1]
    with core.store.transaction():
        paid_budget._view_key(core.store, path, raw)
    assert len(hashes) == (4 if retain_bytes else 5)


def test_frozen_journal_digest_reuse_still_reads_and_rejects_current_file(
    core, tmp_path, monkeypatch,
):
    approval, _ = settled(core, tmp_path)
    path = Path(approval.journal_path)
    raw, original = path.read_bytes(), Path.read_bytes
    reads = []

    def read(value):
        if value == path:
            reads.append(True)
        return original(value)

    monkeypatch.setattr(Path, "read_bytes", read)
    with core.store.transaction():
        paid_budget.journal(core.store, approval)
        paid_budget.journal(core.store, approval)
        assert len(reads) == 2
        path.write_bytes(raw.replace(b'"previous":"0', b'"previous":"9', 1))
        with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
            paid_budget.journal(core.store, approval)
    assert len(reads) >= 3

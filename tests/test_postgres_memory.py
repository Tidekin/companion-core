"""Native lexical nomination and bounded reads before full memory lifecycle adoption."""

from contextlib import closing
from types import SimpleNamespace

import psycopg
import pytest

from companion import memory_candidates, retrieval_work
from companion.privacy import ErasureLedger
from companion.store import Store
from tests.postgres_fixture import isolated_native


@pytest.fixture(scope="module")
def memory_database():
    with isolated_native() as (database, _):
        with database.session() as session:
            session.execute("INSERT INTO runtime_meta (key,value) VALUES ('privacy_epoch','0')")
            session.execute(
                "INSERT INTO memories (memory_id,subject,kind,content,status,origin,confidence,"
                "importance,privacy_scope,created_at,updated_at,fact_key) VALUES "
                "('whale','user','fact','小鲸鱼喜欢吃白饭','CONFIRMED','user_confirmed',1,1,"
                "'private','2026-01-01','2026-01-01',''),"
                "('camera','project','fact','相机收在湖蓝色硬壳箱','CONFIRMED','user_confirmed',1,1,"
                "'private','2026-01-01','2026-01-01','camera_case'),"
                "('literal','project','fact','令牌名为AUTH_TOKEN','CONFIRMED','user_confirmed',1,1,"
                "'private','2026-01-01','2026-01-01',''),"
                "('wildcard_trap','project','fact','令牌名为AUTHTTOKEN','CONFIRMED',"
                "'user_confirmed',1,1,'private','2026-01-01','2026-01-01','')")
            session.execute(
                "INSERT INTO memories (memory_id,subject,kind,content,status,origin,confidence,"
                "importance,privacy_scope,created_at,updated_at) SELECT 'noise_'||n,'user','fact',"
                "'离线压测样本'||n||'：天文馆发行纪念邮票','CONFIRMED','user_confirmed',1,1,"
                "'private','2026-02-01','2026-02-01' FROM generate_series(1,1200) n")
        yield database


@pytest.mark.parametrize(("query", "expected"), [("小鲸鱼", "whale"),
                                                ("CAMERA_CASE", "camera"),
                                                ("AUTH_TOKEN", "literal")])
def test_old_lexical_facts_remain_nominated_after_recent_history_growth(
    memory_database, tmp_path, query, expected,
):
    with closing(Store(memory_database, tmp_path)) as store, retrieval_work.scope() as work:
        rows, lexical = memory_candidates.nominate(SimpleNamespace(store=store), query, (), None)
        assert expected in lexical
        assert expected in {row["memory_id"] for row in rows}
        assert "wildcard_trap" not in lexical
        assert len(rows) <= retrieval_work.FACT_CANDIDATES
        assert all("record_sequence" not in row for row in rows)
        assert not work.receipt()["coverage_limited"] or "candidate_pool" in work.limited


def test_native_nomination_keeps_explicit_scope_and_empty_scope(memory_database, tmp_path):
    with closing(Store(memory_database, tmp_path)) as store:
        for subjects, identities in ((('project',), ['whale']), ((), [])):
            with retrieval_work.scope():
                rows, lexical = memory_candidates.nominate(
                    SimpleNamespace(store=store), "小鲸鱼", subjects, identities)
                assert rows == lexical == []


def test_optional_timeout_preserves_outer_snapshot_and_does_not_promote(
    memory_database, tmp_path, monkeypatch,
):
    monkeypatch.setitem(retrieval_work.LIMITS, "sql_milliseconds", 5)
    with closing(Store(memory_database, tmp_path)) as store:
        attempts = []

        def read():
            attempts.append(store.in_read_snapshot)
            with retrieval_work.scope() as work:
                with pytest.raises(retrieval_work.WorkLimit) as error:
                    work.rows(store, "SELECT pg_sleep(1)", limit=1)
                assert error.value.reason == "sql_milliseconds"
                assert work.receipt()["counts"]["sql_milliseconds"] <= 5
                assert store.one("SELECT 7 AS value") == {"value": 7}
                assert store.one("SHOW statement_timeout") == {"statement_timeout": "0"}
            return True

        assert store.read_or_write(read)
        assert attempts == [True]
        assert not store.in_transaction


def test_optional_reads_never_use_a_live_writer_or_execute_a_data_changing_cte(
    memory_database, tmp_path,
):
    with closing(Store(memory_database, tmp_path)) as store:
        with store.transaction(), retrieval_work.scope() as work:
            store.execute("INSERT INTO runtime_meta (key,value) VALUES ('own-writer','kept')")
            with pytest.raises(retrieval_work.WorkLimit) as error:
                work.rows(store, "SELECT 1", limit=1)
            assert error.value.reason == "nomination_requires_committed_snapshot"
        with pytest.raises(psycopg.errors.FeatureNotSupported), retrieval_work.scope() as work:
            work.rows(store, "WITH changed AS (DELETE FROM runtime_meta RETURNING key) "
                      "SELECT * FROM changed", limit=1)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction), retrieval_work.scope() as work:
            work.rows(store, "SELECT nextval('runtime_meta_record_sequence_seq')", limit=1)
        assert store.one("SELECT value FROM runtime_meta WHERE key='own-writer'") == {
            "value": "kept"}
        assert not store.in_transaction


def test_metadata_replacement_preserves_new_insertion_order(memory_database, tmp_path):
    with closing(Store(memory_database, tmp_path)) as store, store.transaction():
        store.execute("INSERT INTO runtime_meta (key,value) VALUES ('ordering-a','old')")
        store.execute("INSERT INTO runtime_meta (key,value) VALUES ('ordering-b','next')")
        store.execute(
            "INSERT INTO runtime_meta (key,value) VALUES ('ordering-a','new') ON CONFLICT(key) "
            "DO UPDATE SET value=excluded.value,record_sequence=DEFAULT")
        rows = store.rows("SELECT key,value FROM runtime_meta WHERE key LIKE 'ordering-%%' "
                          "ORDER BY record_sequence")
        assert rows == [{"key": "ordering-b", "value": "next"},
                        {"key": "ordering-a", "value": "new"}]


def test_memory_erasure_custody_initializes_with_native_control_scope(memory_database, tmp_path):
    from companion.live_text import _ledger_custody

    ledger = ErasureLedger(tmp_path / "erasures.jsonl")
    settings = SimpleNamespace(erasure_ledger=str(ledger.path))
    with closing(Store(memory_database, tmp_path)) as store:
        store.bootstrap("10001", "2026-01-01T00:00:00+00:00")
        ledger.initialize(store)
        original = ledger.path.read_bytes()
        assert len(_ledger_custody(store, settings)) == 64
        ledger.replay(store)
        assert ledger.path.read_bytes() == original
        with (store.transaction(),
              pytest.raises(RuntimeError, match="ERASURE_CONTROL_WRITE_REQUIRED")):
            ledger.expire_events(store, [], "2026-01-01T00:00:00+00:00")
        assert ledger.path.read_bytes() == original


def test_image_reference_queries_skip_mixed_non_json_metadata(memory_database, tmp_path):
    from companion.memory_references import image_reviews

    with closing(Store(memory_database, tmp_path)) as store:
        store.execute("INSERT INTO runtime_meta (key,value) VALUES "
                      "('memory_reference_input:invalid','not-json'),"
                      "('memory_reference_input:unrelated','{\"kind\":\"transcript\"}')")
        assert image_reviews(store, "evt_synthetic_target") == []
        with retrieval_work.scope() as work:
            assert image_reviews(store, "evt_synthetic_target", work=work) == []

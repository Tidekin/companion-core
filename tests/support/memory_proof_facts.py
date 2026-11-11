"""Synthetic many-fact memory fixture shared by proof batching tests."""

from tests.conftest import ingest


def facts(core, count=96, chars=6000):
    core.settings.memory_enabled = True
    ingest(core, 1, "test_token " + "x" * chars)
    core.store.execute(
        "INSERT INTO events(event_id,schema_version,source,source_event_id,conversation_id,"
        "actor_id,event_type,occurred_at,ingested_at,privacy_scope,body_json,trace_id,"
        "idempotency_key,fingerprint) SELECT 'proof_'||n,e.schema_version,e.source,"
        "e.source_event_id||n,e.conversation_id,e.actor_id,e.event_type,e.occurred_at,"
        "e.ingested_at,e.privacy_scope,e.body_json,e.trace_id,e.idempotency_key||n,"
        "e.fingerprint||n FROM events e CROSS JOIN generate_series(1,%s) n "
        "WHERE e.event_id='evt_test_1'", (count,))
    core.store.execute(
        "INSERT INTO memories(memory_id,subject,kind,content,status,origin,confidence,"
        "importance,privacy_scope,created_at,updated_at) SELECT 'fact_'||n,'user','fact',"
        "'test_token fact '||n,'CONFIRMED','user_confirmed',1,.5,'private',%s,%s "
        "FROM generate_series(1,%s) n", (core.clock(), core.clock(), count))
    core.store.execute("INSERT INTO memory_evidence(memory_id,event_id,polarity,created_at) "
        "SELECT 'fact_'||n,'proof_'||n,1,%s FROM generate_series(1,%s) n", (core.clock(), count))
    return core.store.rows("SELECT * FROM memories ORDER BY memory_id")

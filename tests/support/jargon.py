import json

import httpx

from companion import jargon
from companion.jargon_config import JargonConfig
from companion.model import ModelClient
from tests.conftest import ingest, run
from tests.support.exploration import configure
from tests.support.owner_authority import activate
from tests.support.owner_authority_setup import prepare
from tests.support.watch_ledger import Provider

EXTRACT = {"terms": [{"term": "摸鱼王", "event_ids": ["evt_test_2"]},
                     {"term": "不存在的词", "event_ids": ["evt_test_1"]}]}


def ready(core, tmp_path, *texts):
    configure(core)
    core.settings.jargon = JargonConfig(batch_messages=4)
    policy = prepare(core, tmp_path)
    activate(core, policy.model_copy(update={"purposes": ["memory"],
                                             "memory_scope": "owner_private"}))
    provider = Provider(*[json.dumps(t, ensure_ascii=False) for t in texts])
    core.model = ModelClient(core.settings,
                             httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    return provider


def say(core, number, text):
    """Before activation use the native ingest; afterwards copy its stored shape."""
    template = core.store.one("SELECT * FROM events WHERE event_id='evt_test_1'")
    if template is None or not core.store.one(
            "SELECT 1 FROM owner_authority_revisions LIMIT 1"):
        ingest(core, number, text)
        core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
        return
    body = {**json.loads(template["body_json"]), "text": text}
    core.store.execute(
        "INSERT INTO events(event_id,schema_version,source,source_event_id,conversation_id,"
        "actor_id,event_type,occurred_at,ingested_at,privacy_scope,body_json,trace_id) "
        "VALUES(?,1,?,?,?,'owner','message.received',?,?,'private',?,?)",
        (f"evt_test_{number}", template["source"], f"synthetic-{number}",
         template["conversation_id"], core.clock(), core.clock(),
         json.dumps(body, ensure_ascii=False), f"trace_{number:08d}"))


def step(core):
    return run(jargon.run_once(core.store, core.settings, core.model, core.clock))

"""Exact nomination, native first request and legacy-byte migration."""

import hashlib
import json
import struct

import httpx
import psycopg
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import retrieval_work
from companion.contracts import Candidate
from companion.model import MEMORY_HEADING
from companion.postgres import migrate
from companion.retrieval import INDEX_VERSION, SemanticIndex, encoder
from tests.conftest import run
from tests.postgres_fixture import isolated_database
from tests.sdk_reader_fixture import worker_for
from tests.support.model_stream import chunk
from tests.support.native_chat import enroll, packet
from tests.support.owner_console import client as client
from tests.support.owner_input import captured


def test_upgrade_backfills_valid_bytes_and_preserves_opaque_legacy():
    valid = struct.pack("<512f", -0.75, *([0.25] * 511))
    with isolated_database() as info:
        migrate(info, revision="pg0018_moment_images")
        with psycopg.connect(info.get_secret_value()) as db:
            for mid, raw in (("valid", valid), ("opaque", b"legacy"),
                             ("invalid", struct.pack("<f", float("nan")) + valid[4:])):
                db.execute("INSERT INTO memories(memory_id,subject,kind,content,status,origin,"
                           "confidence,importance,privacy_scope,created_at,updated_at) "
                           "VALUES (%s,'user','fact','x','CONFIRMED','user_confirmed',1,1,"
                           "'private','t','t')", (mid,))
                db.execute("INSERT INTO memory_vectors(memory_id,model_version,content_hash,"
                           "vector) VALUES (%s,%s,'h',%s)", (mid, INDEX_VERSION, raw))
        migrate(info)
        with psycopg.connect(info.get_secret_value()) as db:
            assert db.execute("SELECT vector,embedding::real[] FROM memory_vectors "
                              "WHERE memory_id='valid'").fetchone() == (
                                  valid, [-0.75] + [0.25] * 511)
            assert db.execute("SELECT count(*) FROM memory_vectors "
                              "WHERE embedding IS NULL").fetchone() == (2,)


def test_native_first_request_finds_late_memory_and_erasure_removes_it(
    client, core, tmp_path, monkeypatch,
):
    core.settings.memory_enabled = True
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: encoder(
        str(self.settings.embedding_cache_dir), local_only=True))
    captured(core, tmp_path, purposes=["chat", "memory"], memory_scope="owner_private")
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)

    def send(text):
        response = client.post("/v1/console/chat/messages", headers=headers,
                               json=packet(core, key, authority, text=text))
        assert response.status_code == 202, response.text
        return response.json()

    text = "那台小灰相机一直放在湖蓝色硬壳箱，箱底的铭牌是 MZ-418。"
    decoy = "周四入藏的海报放在档案室里。"
    query = "相機平時收在哪個箱子？"
    event = send(text)["event_id"]
    with core.store.transaction(control_write=True):
        fact = Candidate(subject="user", kind="fact", fact_key="camera", content=text,
                         evidence_event_id=event, evidence_quote=text)
        mid = core.memory.add(fact, core.clock(), confirmed=True)
    # Only the gold has a private source. Every distractor qualifies for vector nomination.
    other = next(core.memory.index.warmup().passage_embed([decoy]))
    decoy_hash = hashlib.sha256(decoy.encode()).hexdigest()
    with psycopg.connect(core.settings.database.get_secret_value()) as db:
        db.execute("INSERT INTO memories(memory_id,subject,kind,content,status,origin,confidence,"
                   "importance,privacy_scope,created_at,updated_at) SELECT 'd'||i,'user','fact',"
                   "%s,'CONFIRMED','user_confirmed',1,1,'private','t','t' "
                   "FROM generate_series(1,5399) i", (decoy,))
        db.execute("INSERT INTO memory_vectors(memory_id,model_version,content_hash,vector,"
                   "embedding) SELECT memory_id,%s,%s,%s,%s::real[]::public.vector FROM memories "
                   "WHERE memory_id<>%s", (INDEX_VERSION, decoy_hash,
                                          other.astype("<f4").tobytes(), other.tolist(), mid))
    row = core.store.one("SELECT * FROM memories WHERE memory_id=%s", (mid,))
    core.memory.index.scores(query, [row])
    job = send(query)["job_id"]
    captured_ids = []

    def provider(request):
        assert request.url.host == "synthetic.invalid"
        facts = [item for m in json.loads(request.content)["messages"]
                 if isinstance(m.get("content"), str) and m["content"].startswith(MEMORY_HEADING)
                 for item in json.loads(m["content"][m["content"].index("{"):])["memories"]]
        captured_ids.append([f["memory_id"] for f in facts])
        body = (chunk({"content": "收到。"}) + chunk(finish="stop")
                + chunk(usage={"prompt_tokens": 50, "completion_tokens": 5}) + b"data: [DONE]\n\n")
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    async def reply():
        async with worker_for(core, provider) as worker:
            assert await worker.run_once("native-vector", job_id=job)

    run(reply())
    assert len(captured_ids) == 1 and mid in captured_ids[0]
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, [event], core.clock())
    with retrieval_work.scope():
        assert not core.memory.index.nominate(query, memory_ids=[mid])
    assert not core.store.one("SELECT 1 FROM memory_vectors WHERE memory_id=%s", (mid,))

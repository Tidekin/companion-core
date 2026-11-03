"""Independent regression evidence for discovery accounting and worker boundaries."""

import json
from contextlib import closing
from datetime import datetime

import httpx
import pytest
from cryptography.fernet import Fernet

from companion import meme_service
from companion.backup import backup, restore
from companion.core import Core
from companion.model import EXTRACT_RULES, ModelClient
from companion.store import Store
from tests.conftest import run
from tests.meme_authority_fixture import admit
from tests.test_meme_service import claim, discovery_environment, reservation, result, submit


def test_restored_discovery_reservation_cannot_refund_unknown_post_snapshot_io(
    core, tmp_path, monkeypatch,
):
    async def prepare():
        async with discovery_environment(core, tmp_path, monkeypatch) as (event, _, _):
            submit(core, event)
            item = claim(core)
            reservation(core, item)
            return item

    item = run(prepare())
    archive, restored_path = tmp_path / "reserved.enc", tmp_path / "restored.sqlite3"
    key = Fernet.generate_key()
    backup(core.settings.database, archive, key)
    assert result(core, item, "UNKNOWN")["state"] == "UNKNOWN"
    restore(archive, restored_path, key, core.settings.erasure_ledger)
    with closing(Store(str(restored_path))) as restored:
        assert restored.one(
            "SELECT 1 FROM runtime_meta WHERE key='cloud_budget_restore_required'",
        )
        # The result endpoint intentionally accepts original witnesses despite a
        # restore/STOP guard, so the restored row itself must remain conservative.
        settled = meme_service.result(
            restored, core.settings, core.clock, item["job_id"],
            meme_service.DiscoveryResult(
                lease_token=item["lease_token"], attempt_key=item["attempt_key"],
                outcome="SETTLED", usage=meme_service.DiscoveryUsage(
                    queries=0, requests=0, download_bytes=0, storage_bytes=0, model_cost=0,
                ),
            ),
        )
        assert settled["state"] == "UNKNOWN"
        assert restored.one(
            "SELECT state,reserved_queries,queries FROM meme_discovery_reservations",
        ) == {"state": "UNKNOWN", "reserved_queries": 1, "queries": 0}


@pytest.mark.parametrize("chat_state", ["SUCCEEDED", "FAILED"])
def test_discovery_jobs_do_not_duplicate_or_enable_memory_extraction(
    core, tmp_path, monkeypatch, chat_state,
):
    calls = []

    def provider(request):
        messages = json.loads(request.content)["messages"]
        assert messages[0]["content"] == EXTRACT_RULES
        calls.append(json.loads(messages[-1]["content"]))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"memories":[]}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    async def scenario():
        async with discovery_environment(core, tmp_path, monkeypatch,
            purposes=("memory",), memory_enabled=True,
            extraction_min_events=1, extraction_idle_seconds=0) as (event, chat, values):
            client, config, plan, now = values
            submit(core, event)
            item = claim(core)
            reservation(core, item)
            assert result(core, item)["state"] == "SETTLED"
            core.store.execute("UPDATE jobs SET state=? WHERE job_id=?",
                               (chat_state, chat["job_id"]))
            assert core.store.rows(
                "SELECT kind,state FROM jobs WHERE event_id=? ORDER BY kind", (event["event_id"],),
            ) == [{"kind": "chat", "state": chat_state},
                  {"kind": "meme_discovery", "state": "SUCCEEDED"}]
            # Successful historical chats remain eligible after a newer turn.
            newer, accepted = await admit(client, config, plan, now, number=2)
            assert accepted.status_code == 202, accepted.text
            core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?",
                               (accepted.json()["job_id"],))
            core.store.execute("UPDATE events SET memory_processed=1 WHERE event_id=?",
                               (newer["event_id"],))
            # HTTP admission uses wall time; start the independent worker after
            # committed ingress rather than guessing how long fixture setup took.
            latest_ingress = core.store.one("SELECT MAX(ingested_at) AS at FROM events")["at"]
            core.clock.now = max(core.clock.now, datetime.fromisoformat(latest_ingress))
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as model_http:
                model = ModelClient(core.settings, model_http)
                with Core(core.settings, model, core.clock) as worker:
                    assert worker.store is not core.store
                    completed = await worker.extract_once()
            source = core.store.one(
                "SELECT conversation_id,occurred_at FROM events WHERE event_id=?",
                (event["event_id"],))
            expected = {"groups": {"g0": ["e0"]}, "events": [
                {"ref": "e0", "text": event["payload"]["text"], "group": "g0",
                 "references": [], "occurred_at": source["occurred_at"]}]}
            assert completed is (chat_state == "SUCCEEDED"), json.dumps({
                "clock": core.clock(),
                "events": core.store.rows(
                    "SELECT event_id,memory_processed,ingested_at FROM events",
                ),
                "jobs": core.store.rows("SELECT job_id,kind,state FROM jobs"),
                "usage": core.store.rows("SELECT scenario,state FROM model_usage"),
                "audit": core.store.rows("SELECT action,safe_detail_json FROM audit_log"),
            })
            assert calls == ([expected] if chat_state == "SUCCEEDED" else [])
            assert core.store.rows("SELECT event_id FROM episode_events") == (
                [{"event_id": event["event_id"]}] if chat_state == "SUCCEEDED" else []
            )

    run(scenario())

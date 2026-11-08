"""Retire a historical settled recall failure without replay, refund or invented ACK."""
import asyncio
from pathlib import Path

import httpx
import pytest

from companion import live_text
from companion.privacy import advance_epoch, epoch
from companion.runtime import Runtime
from companion.store import after, uid
from tests.conftest import ingest, run
from tests.historical_input_retirement import historical_input


@pytest.mark.parametrize("mutation", [
    None, "new_privacy_epoch", "contract_replay", "unknown", "reserved", "missing_admission",
    "outbox",
    "active_job", "lease", "foreign_event", "superseded", "missing_failure", "wrong_failure",
    "wrong_scenario", "erased_source", "not_stopped", "forged_revision", "forged_resolution",
])
def test_historical_failed_recall_retirement_preserves_original_accounting(
    core, tmp_path, mutation,
):
    if mutation == "contract_replay":
        seed = ingest(core, 900, "expired unrelated source",
                      origin=f"seed:FriendMessage:{core.settings.owner_qq_id}")
        with core.store.transaction(control_write=True):
            core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s",
                               (seed["job_id"],))
            core.ledger.expire_events(core.store, [seed["event_id"]], core.clock())
        assert core.store.one("SELECT state FROM privacy_erasure_ledger")["state"] == "COMPLETE"

    async def scenario():
        async with historical_input(core, tmp_path, failure="memory") as (window, client, job):
            core.clock.advance(core.settings.lease_seconds + 1)
            original_job = core.store.one('SELECT * FROM jobs WHERE job_id=%s', (job["job_id"],))
            assert original_job["state"] == "FAILED"
            assert original_job["last_error_code"] == "MEMORY_REVIEW_INVALID"
            usage = core.store.one("SELECT * FROM model_usage")
            assert usage["state"] == "SETTLED" and usage["billed_micro_usd"] > 0
            if mutation == "new_privacy_epoch":
                with core.store.transaction(control_write=True):
                    advance_epoch(core.store)
            elif mutation == "contract_replay":
                from companion.erasure import CONTRACT_KEY, contract_signature

                old_epoch = epoch(core.store)
                ledger = Path(core.settings.erasure_ledger)
                ledger_before = await asyncio.to_thread(ledger.read_bytes)
                core.store.execute("UPDATE runtime_meta SET value='1,legacy' WHERE key=%s",
                                   (CONTRACT_KEY,))
                with Runtime(core.settings, clock=core.clock) as upgraded:
                    assert upgraded.store is not core.store
                    assert epoch(upgraded.store) == old_epoch + 1
                    assert upgraded.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                                              (CONTRACT_KEY,))["value"] == contract_signature()
                assert await asyncio.to_thread(ledger.read_bytes) == ledger_before
            elif mutation in {"unknown", "reserved"}:
                core.store.execute('UPDATE model_usage SET state=%s', (mutation.upper(),))
            elif mutation == "missing_admission":
                core.store.execute("DELETE FROM model_admissions")
            elif mutation == "outbox":
                core.store.insert("outbox", message_id=uid("msg"), origin_event_id=job["event_id"],
                    conversation_id=job["conversation_id"], action_type="qq.private.reply",
                    content_json='{"text":"late"}', content_sha256="a"*64,
                    delivery_key="late-failed-recall", state="FAILED", attempt_count=0,
                    created_at=core.clock(), updated_at=core.clock())
            elif mutation == "active_job":
                core.store.execute("UPDATE jobs SET state='RUNNING' WHERE job_id=%s",
                                   (job["job_id"],))
            elif mutation == "lease":
                core.store.execute('UPDATE jobs SET lease_until=%s WHERE job_id=%s',
                                   (after(core.clock(), 10), job["job_id"]))
            elif mutation == "foreign_event":
                core.store.execute("UPDATE events SET actor_id='companion' WHERE event_id=%s",
                                   (job["event_id"],))
            elif mutation == "superseded":
                core.store.execute("UPDATE conversations SET reply_revision=reply_revision+1")
            elif mutation == "missing_failure":
                core.store.execute("DELETE FROM audit_log WHERE action='memory.recall_invalid'")
            elif mutation == "wrong_failure":
                core.store.execute("UPDATE jobs SET last_error_code='OTHER_FAILURE'")
            elif mutation == "wrong_scenario":
                core.store.execute("UPDATE model_usage SET scenario='chat'")
            elif mutation == "erased_source":
                with core.store.transaction(control_write=True):
                    core.ledger.expire_events(core.store, [job["event_id"]], core.clock())
            elif mutation == "not_stopped":
                core.store.execute('DELETE FROM runtime_meta WHERE key=%s',
                                   (live_text.STOP + window.plan.sha256,))
                core.store.execute("DELETE FROM runtime_meta WHERE key='live_text_stopped'")
            elif mutation in {"forged_revision", "forged_resolution"}:
                original = window.core_post

                async def forged(*args):
                    result = await original(*args)
                    result["reply_revision" if mutation == "forged_revision" else "resolution"] = (
                        True if mutation == "forged_revision" else "ACK")
                    return result

                window.core_post = forged
            models = core.store.rows("SELECT * FROM model_usage")
            admissions = core.store.rows("SELECT * FROM model_admissions")
            jobs = core.store.rows("SELECT * FROM jobs")
            journal = Path(core.settings.paid_model.journal_path)
            budget_before = await asyncio.to_thread(journal.read_bytes)
            head = window.row()["journal_head"]
            if mutation not in {None, "new_privacy_epoch", "contract_replay"}:
                with pytest.raises((ValueError, httpx.HTTPStatusError)):
                    await window.reconcile_cancelled_inputs(client)
                assert window.row()["journal_head"] == head
                assert window.db.execute("SELECT state FROM window_inputs").fetchone()[0] == (
                    "APPROVED")
            else:
                result = await window.reconcile_cancelled_inputs(client)
                assert result["cancelled_inputs"] == 1 and result["new_inputs"] == 1
                assert result["state"] == "STOPPED"
                records, _ = window.read_journal()
                receipt = next(row["data"]["core_receipt"] for row in records
                               if row["kind"] == "INPUT_CANCELLED")
                assert receipt["state"] == "FAILED"
                assert receipt["resolution"] == "SETTLED_NO_DELIVERY"
                assert receipt["failure_code"] == "MEMORY_REVIEW_INVALID"
                assert receipt["failure_phase"] == "memory_recall"
                assert receipt["model_attempts"] == 1 and receipt["send_attempts"] == 0
                assert receipt["billed_micro_usd"] == usage["billed_micro_usd"]
                assert receipt["reply_revision"] == 1
                assert (receipt["input_privacy_epoch"] < receipt["privacy_epoch"]) == (
                    mutation in {"new_privacy_epoch", "contract_replay"})
                assert not any(row["kind"] == "ACK" for row in records)
                assert window.verify(active=False)["state"] == "STOPPED"
                assert (await window.reconcile_cancelled_inputs(client))["cancelled_inputs"] == 0
            assert core.store.rows("SELECT * FROM model_usage") == models
            assert core.store.rows("SELECT * FROM model_admissions") == admissions
            assert core.store.rows("SELECT * FROM jobs") == jobs
            assert await asyncio.to_thread(journal.read_bytes) == budget_before
            assert core.model.calls == []

    run(scenario())

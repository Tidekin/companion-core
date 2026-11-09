"""Recover one owner correction across mutation and Outbox crash boundaries."""

import httpx
import pytest

from companion import owner_authority
from companion.contracts import CoreError
from companion.core import Core
from companion.model import ModelClient
from tests.conftest import run
from tests.support.native_consumer_entry import environment


class ProcessExit(BaseException):
    pass


@pytest.mark.parametrize("stage", [
    "before_mutation", "before_commit", "after_commit", "before_outbox", "after_outbox",
])
def test_correction_crash_recovers_same_fact_and_one_receipt(core, tmp_path, monkeypatch, stage):
    calls = []

    def provider(request):
        calls.append(request)
        raise AssertionError("Memory correction must not call the external model")

    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "memory", "expression"),
            core_options={"expression_enabled": True}) as (entry, _), httpx.AsyncClient(
                transport=httpx.MockTransport(provider)) as transport:
            core.model = ModelClient(core.settings, transport)
            await entry.admit(1, "/记住这个 我喜欢绿茶")
            assert await core.run_once("seed-native-fact")
            mid = core.store.one("SELECT memory_id FROM memories")["memory_id"]
            assert (await entry.send(entry.client, entry.protocol))["worked"]
            source = await entry.admit(2, f"/纠正这一条 {mid} 我喜欢红茶")
            decide, command, enqueue = (core.memory.decide, core.command_reply,
                                        core.expression.enqueue)

            def crash_decide(*args, **kwargs):
                if stage == "before_commit":
                    decide(*args, **kwargs)
                raise ProcessExit()

            async def crash_command(*args):
                await command(*args)
                raise ProcessExit()

            def crash_enqueue(*args, **kwargs):
                if stage == "after_outbox":
                    enqueue(*args, **kwargs)
                raise ProcessExit()

            with monkeypatch.context() as fault:
                if stage in {"before_mutation", "before_commit"}:
                    fault.setattr(core.memory, "decide", crash_decide)
                elif stage == "after_commit":
                    fault.setattr(core, "command_reply", crash_command)
                else:
                    fault.setattr(core.expression, "enqueue", crash_enqueue)
                with pytest.raises(ProcessExit):
                    await core.run_once("before-crash")
            committed = core.store.one(
                'SELECT memory_id FROM memories WHERE supersedes_memory_id=%s', (mid,),
            )
            assert bool(committed) == (stage not in {"before_mutation", "before_commit"})
            assert not core.store.rows('SELECT * FROM outbox WHERE origin_event_id=%s',
                                       (source["event_id"],))
            core.clock.advance(core.settings.lease_seconds + 1)
            with Core(core.settings, core.model, core.clock) as restarted:
                assert await restarted.run_once("after-crash")
                result = restarted.store.one(
                    'SELECT memory_id,status FROM memories WHERE supersedes_memory_id=%s', (mid,),
                )
                assert result["status"] == "CONFIRMED"
                if committed:
                    assert result["memory_id"] == committed["memory_id"]
                assert restarted.store.one("SELECT COUNT(*) n FROM memories")["n"] == 2
                assert restarted.store.one("SELECT COUNT(*) n FROM memory_revisions")["n"] == 1
                outbox = restarted.store.rows((
                    'SELECT content_json,state FROM outbox WHERE '
                    'origin_event_id=%s'
                ), (source["event_id"],))
                assert len(outbox) == 1 and outbox[0]["state"] == "PENDING"
                assert result["memory_id"] in outbox[0]["content_json"]
                assert not restarted.store.rows("SELECT * FROM jobs WHERE state<>'SUCCEEDED'")
                assert not await restarted.run_once("already-complete")
                assert restarted.store.one(
                    "SELECT COUNT(*) n FROM events WHERE event_type='memory.corrected'",
                )["n"] == 1
                assert restarted.store.one("SELECT COUNT(*) n FROM event_dependencies "
                    "WHERE event_id IN (SELECT event_id FROM events "
                    "WHERE event_type='memory.corrected')")["n"] == 1
            assert calls == [] and not core.store.rows("SELECT * FROM model_usage")

    run(scenario())


@pytest.mark.parametrize("boundary", ["stop", "expiry", "forget", "lease"])
def test_correction_replay_rechecks_authority_and_cannot_recreate_erased_fact(
    core, tmp_path, boundary,
):
    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "memory"),
            expires_in=60 if boundary == "expiry" else 3600) as (entry, _):
            await entry.admit(1, "/记住这个 我喜欢绿茶")
            assert await core.run_once("seed-native-fact")
            mid = core.store.one("SELECT memory_id FROM memories")["memory_id"]
            assert (await entry.send(entry.client, entry.protocol))["worked"]
            source = await entry.admit(2, f"/纠正这一条 {mid} 我喜欢红茶")
            job = core.claim_job("before-boundary")
            if boundary == "forget":
                assert "CONFIRMED" in await core.command_reply(job)
                revised = core.store.one(
                    'SELECT memory_id FROM memories WHERE supersedes_memory_id=%s', (mid,),
                )["memory_id"]
                core.memory.decide(revised, "forget", core.clock())
            elif boundary == "stop":
                await entry.stop()
            elif boundary == "expiry":
                core.clock.advance(61)
            else:
                core.clock.advance(core.settings.lease_seconds + 1)
                assert owner_authority.current(core.store, core.settings, core.clock
                    ).authority.as_dict() == entry.runtime.authority
            with pytest.raises(CoreError) as failure:
                await core.command_reply(job)
            if boundary == "lease":
                assert failure.value.code == "JOB_LEASE_LOST"
            elif boundary == "expiry":
                assert failure.value.code == "OWNER_AUTHORITY_EXPIRED"
            assert not core.store.rows('SELECT * FROM outbox WHERE origin_event_id=%s',
                                       (source["event_id"],))
            if boundary == "forget":
                assert core.store.one(
                    "SELECT COUNT(*) n FROM memories WHERE content<>'' OR status<>'FORGOTTEN'",
                )["n"] == 0
            else:
                assert core.store.one("SELECT COUNT(*) n FROM memories")["n"] == 1
            if boundary == "stop":
                with pytest.raises(CoreError, match="OWNER_AUTHORITY"):
                    owner_authority.current(core.store, core.settings, core.clock)

    run(scenario())

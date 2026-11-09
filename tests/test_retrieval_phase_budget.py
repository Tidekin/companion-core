"""Phase headroom shares the reply ledger and cannot bypass live tool authority."""
import asyncio

import pytest

from companion import retrieval_work
from companion.agent_tools import core_query_tools
from companion.contracts import CoreError
from tests.conftest import run
from tests.support.native_consumer_entry import environment


def test_nested_phase_headroom_survives_await_without_reset_or_leaking():
    async def scenario():
        with retrieval_work.scope() as work:
            with work.stage("nomination", reserve={"sql_milliseconds": 200}):
                assert work.take("sql_milliseconds", 800)
                with work.stage("nested", reserve={}):
                    assert not await asyncio.to_thread(work.take, "sql_milliseconds")
                assert not work.exhausted()
            assert work.remaining("sql_milliseconds") == 200
            with retrieval_work.scope() as nested:
                assert nested is work
                assert work.take("sql_milliseconds", 200)
            assert work.exhausted() and work.counts["sql_milliseconds"] == 1000
            assert "stage:nested:sql_milliseconds" in work.receipt()["reasons"]
        with retrieval_work.scope() as other:
            assert other.remaining("sql_milliseconds") == 1000
    run(scenario())


def test_nested_reserve_is_relative_to_the_inherited_effective_ceiling():
    async def scenario():
        with retrieval_work.scope() as work:
            with work.stage("automatic", reserve={"sql_milliseconds": 150}):
                assert work.take("sql_milliseconds", 600)
                with work.stage("proofs", reserve={"sql_milliseconds": 83}):
                    assert work.remaining("sql_milliseconds") == 167
                    assert await asyncio.to_thread(work.take, "sql_milliseconds", 167)
                    assert not work.take("sql_milliseconds")
                assert work.remaining("sql_milliseconds") == 83
                assert work.take("sql_milliseconds", 83)
            assert work.remaining("sql_milliseconds") == 150
            assert work.counts["sql_milliseconds"] == 850
    run(scenario())


def test_exhausted_recall_is_terminal_and_still_checks_current_owner_authority(core, tmp_path):
    async def scenario():
        async with environment(core, tmp_path) as (entry, _):
            event = await entry.admit(1, "以前说过的铜盒在哪")
            job = core.claim_job("bounded-tool", job_id=entry.accepted[event["event_id"]]["job_id"])
            tool = core_query_tools(core, job, []).tools["memory_recall"].function
            with retrieval_work.scope() as work:
                assert work.take("sql_milliseconds", 1000)
                for query in ("铜盒", "盒子", "收纳"):
                    value = await tool(query)
                    assert value["state"] == "STOPPED"
                    assert value["stop_reason"] == "WORK_EXHAUSTED"
                    assert value["search_can_continue"] is False
                    assert value["retrieval_coverage"]["counts"]["searches"] == 0
                await entry.stop()
                with pytest.raises(CoreError):
                    await tool("铜盒")
            assert not core.store.rows("SELECT * FROM model_usage")
    run(scenario())

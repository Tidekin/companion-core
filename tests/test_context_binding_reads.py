"""Native request bindings stay indexed, batched and checked after each mutation."""

import asyncio
import json

import httpx
import pytest
from pydantic_ai.toolsets import FunctionToolset

from companion import agent_runtime, context_planner
from companion.agent_persistence import ConversationStore
from companion.contracts import CoreError
from companion.model import ModelClient
from tests.sdk_reader_fixture import Provider, answer, call
from tests.support.context_planner import initial
from tests.support.owner_runtime import chat


def test_tool_request_bindings_read_once_and_reject_each_changed_row(
    core, tmp_path, monkeypatch,
):
    _, job = chat(core, tmp_path)
    messages, snapshots = initial(core, job)
    persistence = ConversationStore(core, job, snapshots)

    def lookup() -> str:
        """Return a synthetic tool result."""
        return "synthetic result"

    provider = Provider(answer(calls=[call("binding-tool", "lookup", {})]), answer("done"))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            core.model = ModelClient(core.settings, client)
            result = await agent_runtime.run_conversation(
                core, job, messages, toolsets=[FunctionToolset([lookup])],
                snapshots=snapshots, persistence=persistence,
            )
            assert result.output == "done"

    asyncio.run(scenario())
    queries = []
    execute = core.store._session._execute

    def observed(cursor, statement, parameters=(), **kwargs):
        if "model_admissions AS a JOIN model_usage AS m" in str(statement):
            queries.append((statement, parameters))
        return execute(cursor, statement, parameters, **kwargs)

    monkeypatch.setattr(core.store._session, "_execute", observed)
    plan = context_planner.check(core, job, snapshots)
    assert len(plan["requests"]) == len(provider.requests) == 2
    assert len(queries) == 1
    statement, parameters = queries[0]
    assert set(core.store.rows(statement, parameters)[0]) == {
        "request_id", "job_id", "payload_sha256", "measurement_json",
    }
    core.store.execute(
        "INSERT INTO jobs(job_id,kind,state,idempotency_key,created_at,updated_at) "
        "VALUES ('foreign-binding','memory_extract','SUCCEEDED','foreign-binding',%s,%s)",
        (core.clock(), core.clock()),
    )
    for record in plan["requests"]:
        request_id = record["request_id"]
        for mutation in (
            "UPDATE model_admissions SET payload_sha256='changed' WHERE request_id=%s",
            "UPDATE model_admissions SET measurement_json='{}' WHERE request_id=%s",
            "DELETE FROM model_admissions WHERE request_id=%s",
            "UPDATE model_usage SET job_id='foreign-binding' WHERE request_id=%s",
        ):
            with core.store.transaction(rollback=True):
                core.store.execute(mutation, (request_id,))
                with pytest.raises(CoreError, match="CONTEXT_PLAN_INVALID"):
                    context_planner.check(core, job, snapshots)
        assert context_planner.check(core, job, snapshots) == plan


def test_binding_lookup_does_not_scan_unrelated_requests(core, monkeypatch, record_property):
    # The real check_bound query runs against connection-local tables with the
    # same indexed keys. No artificial financial rows enter native custody.
    core.store.execute("CREATE TEMP TABLE model_admissions (request_id text PRIMARY KEY, "
                       "payload_sha256 text,measurement_json text)")
    core.store.execute("CREATE TEMP TABLE model_usage (request_id text PRIMARY KEY,job_id text)")
    core.store.execute("INSERT INTO model_admissions SELECT n::text,'payload','{}' "
                       "FROM generate_series(1,10000) n")
    core.store.execute("INSERT INTO model_usage SELECT n::text,'job' "
                       "FROM generate_series(1,10000) n")
    core.store.execute("ANALYZE model_admissions")
    core.store.execute("ANALYZE model_usage")
    from companion.paid_budget import sha

    plan = {"requests": [{"request_id": "5000", "payload_sha256": "payload",
                          "measurement_sha256": sha({})}]}
    monkeypatch.setattr(context_planner, "validate", lambda *args: plan)
    queries = []
    rows = core.store.rows

    def observed(statement, parameters=()):
        queries.append((statement, parameters))
        return rows(statement, parameters)

    monkeypatch.setattr(core.store, "rows", observed)
    assert context_planner.check_bound(core.store, {"job_id": "job"}, [], 0) == plan
    assert len(queries) == 1
    statement, parameters = queries[0]
    query_plan = core.store.one("EXPLAIN (ANALYZE, FORMAT JSON) " + statement,
                                parameters)["QUERY PLAN"][0]

    def scans(node):
        if node.get("Relation Name") in {"model_admissions", "model_usage"}:
            yield node
        for child in node.get("Plans", []):
            yield from scans(child)

    bindings = list(scans(query_plan["Plan"]))
    record_property("request_binding_query_plan", json.dumps(query_plan))
    assert len(bindings) == 2
    assert all(scan["Actual Rows"] == 1 for scan in bindings)
    assert all(scan.get("Rows Removed by Filter", 0) < 100 for scan in bindings)

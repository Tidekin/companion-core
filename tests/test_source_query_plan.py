"""Source custody must not scan unrelated native jobs as history grows."""

import json

from companion import owner_input
from tests.conftest import ingest


def test_source_binding_looks_up_its_job_without_scanning_the_job_history(core, record_property):
    ingest(core, 1, "source lookup")
    core.store.execute(
        "INSERT INTO jobs(job_id,kind,state,idempotency_key,created_at,updated_at) "
        "SELECT 'unrelated_'||n,'memory_extract','SUCCEEDED','unrelated_'||n,%s,%s "
        "FROM generate_series(1,10000) n", (core.clock(), core.clock()))
    core.store.execute("ANALYZE jobs")
    plan = core.store.one("EXPLAIN (ANALYZE, FORMAT JSON) " + owner_input._SOURCE_SQL,
                           ("evt_test_1",))["QUERY PLAN"][0]

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    bindings = [node for node in nodes(plan["Plan"]) if node.get("Relation Name") == "jobs"]
    assert len(bindings) == 1
    scan = bindings[0]
    receipt = {key: scan.get(key) for key in (
        "Node Type", "Index Name", "Actual Rows", "Rows Removed by Filter", "Actual Loops")}
    receipt["execution_ms"] = plan["Execution Time"]
    record_property("source_job_query_plan", json.dumps(receipt))
    assert scan["Actual Rows"] == 1
    assert scan.get("Rows Removed by Filter", 0) < 100

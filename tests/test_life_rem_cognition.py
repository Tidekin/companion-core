"""Owner HTTP observes produced Life fiction through its original REM source proof."""

import httpx
import pytest

from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.life_rem_consumption import draft, prepared


@pytest.mark.parametrize("change", ["none", "source", "output", "recall", "queued"])
def test_produced_life_creation_has_read_only_owner_cognition(core, tmp_path, change):
    async def scenario():
        async with prepared(core, tmp_path, automatic=True) as (_, capture, recall, _):
            retained = await draft(core, tmp_path, capture, independent=change == "none")
            if change != "none":
                assert core.life_topics.consume_due(diary=core.diary)
                job = core.store.one("SELECT job_id FROM rem_records")
                if change == "queued":
                    core.store.execute((
                        "UPDATE life_activity_results SET text='changed' "
                        'WHERE job_id=%s'
                    ), (retained["job_id"],))
                assert await core.run_once(job_id=job["job_id"])
            record = core.store.one("SELECT * FROM rem_records")
            usage = core.store.rows("SELECT * FROM model_usage")
            assert len(usage) == 1 and usage[0]["state"] == "SETTLED"
            assert usage[0]["job_id"] == retained["job_id"]
            if change == "source":
                core.store.execute("UPDATE rem_records SET source_event_ids_json='[\"changed\"]'")
            elif change == "output":
                core.store.execute("UPDATE rem_records SET output_json="
                                   "jsonb_set(output_json::jsonb,'{text}',to_jsonb('changed'::text))::text")
            elif change == "recall":
                result = await recall(capture)
                assert result.status_code == 200, result.text
            async with real_core_http(core) as port, httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False,
            ) as client:
                with core.store.read_snapshot():
                    query = {"kind": "rem", "record_id": record["rem_id"]}
                    for headers in ({}, {"Authorization": "Bearer "
                                         + core.settings.bridge_token.get_secret_value()}):
                        response = await client.post("/internal/v2/cognition", json=query,
                                                     headers=headers)
                        assert response.status_code == 401
                    response = await client.post("/internal/v2/cognition", json=query,
                        headers={"Authorization": "Bearer synthetic-life-rem-control"})
                assert response.status_code == 200, response.text
                assert response.headers["cache-control"] == "no-store"
                item = response.json()["items"][0]
                assert item["id"] == record["rem_id"] and item["job_id"] == record["job_id"]
                assert item["kind"] == "FICTION" and item["audience"] == "owner_only"
                assert item["content_kind"] == "private_fiction"
                assert item["source_available"] is (change == "none")
                assert item["source_memory_ids"] == []
                assert item["usage"] is None and item["usage_status"] == "NOT_RECORDED"
                if change == "queued":
                    assert item["state"] == "FAILED"
                    assert item["last_error_code"] == "LIFE_DRAFT_CHANGED"
                if change == "none":
                    assert capture.event_id in item["source_event_ids"]
                    assert item["decision"] == "DREAM"
                    assert item["questions"] == {"state": "UNVERIFIED", "count": 0}
                else:
                    assert item["source_event_ids"] == []
                    assert item["decision"] == "NOT_AVAILABLE"
                assert retained["text"] not in response.text
                assert core.store.rows("SELECT * FROM model_usage") == usage
                assert len(core.store.rows("SELECT * FROM rem_records")) == 1

    run(scenario())

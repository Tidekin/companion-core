"""Original native Reader identity stays fixed while late I/O metering can settle."""

import pytest

from tests.conftest import run
from tests.support.exploration import URL
from tests.support.reading_followup import (
    READER_AUTH,
    admit,
    finish,
    lease,
    select_read,
    validate,
    window,
)


@pytest.mark.parametrize("drift,code", [
    ("target", "READER_TARGET_MISMATCH"), ("source", "OWNER_SOURCE_CHANGED"),
])
@pytest.mark.parametrize("uncertain", [False, True])
def test_native_reader_rejects_changed_identity_but_retains_original_usage(
    core, tmp_path, monkeypatch, drift, code, uncertain,
):
    async def scenario():
        async with window(core, tmp_path, monkeypatch) as (client, config, plan):
            event, _ = await admit(client, core, config, plan, 1, card=URL)
            selector = await select_read(core, URL)
            original_usage = core.store.rows("SELECT * FROM model_usage")
            item = await lease(client)
            reservation = core.store.one("SELECT * FROM exploration_requests")
            if drift == "target":
                core.store.execute("UPDATE exploration_requests SET url=? WHERE request_id=?",
                                   (URL + "7", item["request_id"]))
            else:
                core.store.execute("UPDATE events SET source=? WHERE event_id=?",
                                   ("observation.bilibili", event["event_id"]))
            checked = await validate(client, item)
            assert checked.status_code in {403, 409}
            assert checked.json() == {"error": code}
            outcome, body = await finish(client, core, item,
                status="UNKNOWN" if uncertain else "SUCCESS")
            assert outcome == {"state": "UNKNOWN" if uncertain else "CANCELLED",
                               "event_ids": [], "duplicate": False}
            row = core.store.one("SELECT * FROM exploration_requests")
            assert (row["reserved_requests"], row["reserved_bytes"]) == (
                reservation["reserved_requests"], reservation["reserved_bytes"])
            assert (row["used_requests"], row["used_bytes"]) == (
                (None, None) if uncertain else (body["used_requests"], body["used_bytes"]))
            attempt = core.store.one("SELECT * FROM reading_attempts")
            assert attempt["observed_requests"] == body["observed_requests"]
            assert attempt["observed_bytes"] == body["observed_bytes"]
            duplicate = await client.post(
                "/internal/v3/readers/bili/" + item["request_id"] + "/result",
                json=body, headers=READER_AUTH)
            assert duplicate.status_code == 200
            assert duplicate.json() == {**outcome, "duplicate": True}
            assert core.store.one("SELECT * FROM reading_attempts") == attempt
            assert core.store.rows("SELECT * FROM model_usage") == original_usage
            assert len(selector.requests) == 1
            assert not core.store.one("SELECT 1 FROM observations")
            assert await lease(client) is None
    run(scenario())

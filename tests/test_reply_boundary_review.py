"""Independent review regressions for input lifecycle HTTP boundaries."""

import json

from companion import owner_input
from tests.conftest import run
from tests.native_input_protocol_fixture import admit, environment, notice


def test_unrelated_recall_preserves_exact_input_approval_replay(core, tmp_path, monkeypatch):
    async def scenario():
        async with environment(core, tmp_path, monkeypatch) as (client, config, plan, now):
            _, first = await admit(client, config, plan, now)
            assert first.status_code == 202, first.text
            event, second = await admit(client, config, plan, now, 2)
            assert second.status_code == 202, second.text
            key = owner_input.INPUT + event["event_id"]
            approved = core.store.one((
                'SELECT value FROM runtime_meta WHERE key=%s'
            ), (key,))["value"]
            replay = json.loads(approved)["capture"]

            recalled = await client.post(
                "/internal/v1/owner-authority/inputs/recall", json=notice(config, plan, now),
            )
            assert recalled.status_code == 200, recalled.text
            assert recalled.json()["state"] == "RECALLED"
            assert core.store.one('SELECT redacted FROM events WHERE event_id=%s',
                                  (event["event_id"],))["redacted"] == 0

            repeated = await client.post("/internal/v1/owner-authority/inputs/approve", json=replay)
            assert repeated.status_code == 200, repeated.text
            assert repeated.json() == {"approved": True, "duplicate": True}
            assert core.store.one(
                'SELECT value FROM runtime_meta WHERE key=%s', (key,))["value"] == approved

    run(scenario())

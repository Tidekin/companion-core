"""Life produces and adjusts native plans across independent Core/Reader processes."""

import asyncio
import json
import os
import subprocess
import sys

import httpx
import pytest
from pydantic import SecretStr

from companion import owner_authority
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.goal_read_plan import URLS
from tests.support.life_read_plan import prepare
from tests.support.social_owner_authority import close_provider_clients as close_provider_clients


async def life_child(core, tmp_path, phase, number):
    config, output = tmp_path / f"plan-{number}.json", tmp_path / f"plan-{number}-result.json"
    config.write_text(json.dumps({
        "settings": core.settings.model_dump(mode="python"), "now": core.clock(),
        "phase": phase, "result_path": str(output),
    }, default=lambda value: value.get_secret_value() if isinstance(value, SecretStr)
       else str(value)), encoding="utf-8")
    process = await asyncio.to_thread(subprocess.run,
        [sys.executable, "-B", "-m", "tests.life_plan_process", str(config)],
        capture_output=True, timeout=60)
    assert process.returncode == 0, process.stderr.decode(errors="replace")
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["pid"] != os.getpid()
    return result


async def reader_child(core, port, url):
    process = await asyncio.to_thread(subprocess.run,
        [sys.executable, "-B", "-m", "tests.goal_plan_reader_process",
         "--base", f"http://127.0.0.1:{port}", "--now", core.clock(), "--url", url],
        capture_output=True, timeout=60)
    assert process.returncode == 0, process.stderr.decode(errors="replace")
    result = json.loads(process.stdout.decode().splitlines()[-1])
    assert result["worked"] and result["calls"] == 3, result


@pytest.mark.parametrize("phase,channel", [
    ("FINISH", "qq.private.proactive"), ("DIARY", "diary.save"),
])
def test_native_life_proposes_reorders_reads_and_finishes_one_plan(core, tmp_path, phase, channel):
    prepare(core, tmp_path, channel=channel)

    async def scenario():
        async with real_core_http(core) as port:
            first = await life_child(core, tmp_path, "PROPOSE", 1)
            assert first["provider_calls"] == 1, first
            goal = core.store.one("SELECT * FROM goals")
            assert goal and json.loads(goal["scope_json"])["urls"] == URLS, first
            steps = core.store.rows("SELECT * FROM goal_steps ORDER BY position")
            assert [step["state"] for step in steps] == ["RUNNING", "WAITING_DECISION",
                                                          "WAITING_DECISION"]
            assert core.store.rows("SELECT * FROM trait_evidence") == []
            original_ids = {step["step_id"] for step in steps}
            for index, (url, next_phase) in enumerate(zip(
                [URLS[0], URLS[2], URLS[1]], ["REORDER", "CONTINUE", phase], strict=True,
            ), 2):
                await reader_child(core, port, url)
                parent = core.goals.get(goal["goal_id"])
                finished = index == 4
                assert parent["state"] == ("DONE" if finished else "WAITING_INPUT")
                core.clock.advance(61)
                result = await life_child(core, tmp_path, next_phase, index)
                assert result["provider_calls"] == 1, result
                assert all(job["state"] != "FAILED" for job in result["jobs"]), result
                if not finished:
                    assert core.store.rows("SELECT * FROM trait_evidence") == []
            final = core.goals.plan(goal["goal_id"])
            assert final["state"] == "DONE"
            assert {step["step_id"] for step in final["steps"]} == original_ids
            assert [step["scope"]["url"] for step in final["steps"]] == [URLS[0], URLS[2], URLS[1]]
            assert all(step["state"] == "DONE" for step in final["steps"])
            assert len(core.store.rows("SELECT * FROM exploration_requests")) == 3
            interests = result["materials"][0]["decision_context"]["self_interests"]
            # The owner's message supplied these links: a starting point, not her own pick.
            assert len(interests) == 1 and (interests[0]["independent_sessions"],
                                            interests[0]["owner_suggested_sessions"]) == (0, 1)
            guidance = result["materials"][0]["decision_context"]["autonomy_guidance"]
            assert guidance["depth_control"][0]["owner_suggested_sessions"] == 1
            assert len(core.store.rows("SELECT * FROM trait_evidence")) == 1
            source_job = next(row for row in result["results"] if row["destination"] != "NONE")
            if channel == "diary.save":
                entries = core.store.rows("SELECT * FROM diary_entries")
                assert len(entries) == 1 and entries[0]["body_md"] == source_job["text"]
                # No judged read recorded a change of view, so no interest shift is claimed.
                assert "我发现自己想继续探索" not in entries[0]["body_md"]
                assert not core.store.rows("SELECT * FROM outbox")
            else:
                outbox = core.store.rows("SELECT * FROM outbox")
                assert len(outbox) == 1 and outbox[0]["action_type"] == channel
                assert json.loads(outbox[0]["content_json"])["text"] == source_job["text"]
                ref = owner_authority.current(core.store, core.settings, core.clock,
                                               purpose="life").authority.as_dict()
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                    headers={"Authorization": "Bearer "
                             + core.settings.bridge_token.get_secret_value()}) as client:
                    leased = await client.post("/internal/v1/deliveries/lease",
                        json={"worker_id": "autonomy-private-share", "authority": ref})
                    assert leased.status_code == 200, leased.text
                    item = leased.json()["items"][0]
                    path = "/internal/v1/deliveries/" + item["message_id"]
                    begun = await client.post(path + "/begin-send",
                                               json={"lease_token": item["lease_token"]})
                    assert begun.status_code == 200, begun.text
                    sent = await client.post(path + "/result",
                        json={"lease_token": item["lease_token"], "result": "SENT"})
                    assert sent.status_code == 200, sent.text
                assert core.store.one("SELECT state FROM outbox")["state"] == "SENT"
            retained = {table: core.store.rows("SELECT * FROM " + table)
                        for table in ("diary_entries", "outbox", "model_usage", "trait_evidence")}
            restarted = await life_child(core, tmp_path, phase, 5)
            assert restarted["provider_calls"] == 0
            assert len(core.store.rows("SELECT * FROM model_usage")) == 4
            assert {table: core.store.rows("SELECT * FROM " + table)
                    for table in retained} == retained
    run(scenario())


def test_native_life_waits_then_abandons_without_reading_tail(core, tmp_path):
    prepare(core, tmp_path)

    async def scenario():
        async with real_core_http(core) as port:
            first = await life_child(core, tmp_path, "PROPOSE", 1)
            assert first["provider_calls"] == 1, first
            await reader_child(core, port, URLS[0])
            core.clock.advance(61)
            waited = await life_child(core, tmp_path, "WAIT", 2)
            assert waited["provider_calls"] == 1, waited
            assert not core.store.rows("SELECT * FROM jobs WHERE state='FAILED'"), waited
            idle = await life_child(core, tmp_path, "ABANDON", 3)
            assert idle["provider_calls"] == 0, idle
            core.clock.advance(3601)
            abandoned = await life_child(core, tmp_path, "ABANDON", 4)
            assert abandoned["provider_calls"] == 1, abandoned
            goal = core.store.one("SELECT goal_id,state FROM goals")
            assert goal["state"] == "CANCELLED", abandoned
            assert [step["state"] for step in core.goals.plan(goal["goal_id"])["steps"]] == [
                "DONE", "CANCELLED", "CANCELLED",
            ]
            assert len(core.store.rows("SELECT * FROM exploration_requests")) == 1
            assert not core.store.rows("SELECT * FROM trait_evidence")
            assert len(core.store.rows("SELECT * FROM model_usage")) == 3
    run(scenario())

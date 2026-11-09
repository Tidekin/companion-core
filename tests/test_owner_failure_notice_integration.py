"""Real owner HTTP ingress, an independent CLI worker and checked OneBot delivery."""

import json

import pytest

from companion import owner_delivery
from tests.conftest import run
from tests.support.cumulative_budget import view
from tests.support.failure_notice import assert_failure_notice
from tests.support.native_consumer_entry import environment
from tests.support.trial_limits import limits, native_trial
from tests.support.worker_wakeup_process import close_child, finish, ready, start, state


@pytest.mark.parametrize("boundary", ["send", "stop", "recall", "supersede"])
def test_failed_chat_notice_crosses_native_worker_and_send_checks(core, tmp_path, boundary):
    async def scenario():
        async with environment(core, tmp_path, purposes=("chat",),
            paid_options={"max_input_tokens": 1}) as (entry, sends):
            denied = await entry.client.post("/internal/v1/inbox/events", json={},
                headers={"Authorization": "Bearer wrong"})
            assert denied.status_code == 401
            event = await entry.admit(1, "Synthetic message above the approved model bound")
            job_id = entry.accepted[event["event_id"]]["job_id"]
            directory = tmp_path / "failure-worker"
            child = await start(core, directory, duration=30, controlled=True)
            try:
                await ready(child)
                await state(core, job_id, wanted="FAILED", seconds=15)
                notice = assert_failure_notice(core, event["event_id"])
                (directory / "release").touch()
                report = await finish(child)
            finally:
                await close_child(child)
            assert report["total"]["provider_calls"] == 0
            assert core.store.rows("SELECT * FROM model_usage") == []
            if boundary == "supersede":
                await entry.admit(2, "A newer owner turn")
            checks = []

            async def before_send(request):
                if request.url.path.endswith("/owner-authority/send-check"):
                    checks.append(json.loads(request.content))
                    if boundary == "stop":
                        await entry.stop()
                    elif boundary == "recall":
                        await entry.recall(1)

            entry.client.event_hooks["request"].append(before_send)
            result = await entry.send(entry.client, entry.protocol)
            assert len(sends) == int(boundary == "send")
            assert len(checks) == int(boundary != "supersede")
            if boundary == "send":
                assert result["worked"] and result["attempt_states"] == {"ACK": 1}
                assert owner_delivery.FAILURE_TEXT in json.dumps(sends, ensure_ascii=False)
                assert core.store.one('SELECT state FROM outbox WHERE message_id=%s',
                    (notice["message_id"],))["state"] == "SENT"
                assert not (await entry.send(entry.client, entry.protocol))["worked"]
                assert len(sends) == 1
            else:
                assert result["attempt_states"].get("ACK", 0) == 0
            assert core.store.rows("SELECT * FROM model_usage") == []
    run(scenario())


def test_failure_notice_cannot_bypass_exhausted_send_quota(core, tmp_path):
    async def scenario():
        caps = limits(max_inputs=3, max_model_attempts=1,
                      max_send_attempts=1, max_visible_replies=1)
        async with native_trial(core, tmp_path, caps) as (
            active, _, calls, sends, capture, finish_reply,
        ):
            await capture()
            assert await finish_reply()
            await capture()
            assert await core.run_once("notice-without-send-quota")
            failed = core.store.one("SELECT event_id FROM jobs WHERE state='FAILED'")
            assert_failure_notice(core, failed["event_id"])
            assert await active.pump()
            assert len(calls) == len(sends) == 1
            assert sum(key.startswith("send:") for key in view(core)["operations"]) == 1
            assert len(view(core)["models"]) == 1
    run(scenario())

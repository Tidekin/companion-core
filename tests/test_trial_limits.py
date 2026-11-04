"""One numeric profile, independent counters, and the original fail-closed window."""

import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError

from companion import cumulative
from companion.trial_limits import TrialLimits
from companion_onebot_bridge.window import WindowPlan
from scripts.trial_limits import load_limits
from tests.conftest import run
from tests.support.failure_notice import assert_failure_notice
from tests.test_cumulative_budget import policy as quota_policy
from tests.test_cumulative_budget import view
from tests.test_live_continuity import native_continuity
from tests.test_live_text_window import live_fixture
from tests.test_onebot_window import plan_fixture
from tests.test_paid_model_gate import approved_live

ROOT = Path(__file__).resolve().parents[1]


def limits(**changes):
    return TrialLimits.model_validate({
        "max_inputs": 16, "max_model_attempts": 21, "max_send_attempts": 20,
        "max_visible_replies": 18, **changes,
    })


def configured(core, tmp_path, value, *, paid_options=None):
    projected = value.project()
    return live_fixture(core, tmp_path, window_options=projected["bridge_plan"],
                        paid_options={**projected["paid_model"], **(paid_options or {})},
                        **projected["live_text_window"])



def native_quota(core, tmp_path, *, account=None, window=None, paid=None):
    # Real existing cumulative policy/journal; there is no fixture-side counter.
    values = {"max_output_tokens": 4096, "per_attempt_micro_usd": 100000,
        "per_request_micro_usd": 200000, "daily_calls": 100, "window_calls": 100, **(paid or {})}
    approved_live(core, tmp_path, **values)
    scope = quota_policy(core, **({"account": account} if account is not None else {}),
                         **({"window": window} if window is not None else {}))
    with core.store.transaction():
        cumulative.activate(core.store, core.settings, core.clock())
    return {**values, "cumulative": scope}


@asynccontextmanager
async def native_trial(core, tmp_path, value, *, paid=None, texts=None, respond=None, turns=0):
    # TrialLimits' two numeric delivery ceilings become the single approved send dimension.
    # Native authority has independent input, model and actual-send accounting.
    paid_options = native_quota(core, tmp_path, paid=paid, account={
        "owner_inputs": value.max_inputs, "model_attempts": value.max_model_attempts,
        "send_attempts": min(value.max_send_attempts, value.max_visible_replies)})
    async with native_continuity(core, tmp_path, turns=turns,
        texts=texts or [f"第{n}个合成输入" for n in range(1, value.max_inputs + 2)],
        respond=respond, paid_options=paid_options,
        grant_options={"max_input_chars": value.max_input_chars,
                       "max_reply_chars": value.max_reply_chars},
    ) as result:
        # Signed receipts and cumulative accounting share this quota fixture clock.
        receipt_clock = SimpleNamespace(now=lambda _: core.clock.now)
        with patch("companion_onebot_bridge.app.datetime", receipt_clock), patch(
            "tests.native_continuous_support.datetime", receipt_clock,
        ):
            yield result


def test_profile_projects_independent_limits_without_creating_authority(tmp_path):
    projected = limits(max_input_chars=1000, max_reply_chars=1500).project()
    assert projected["bridge_plan"]["max_new_inputs"] == 16
    assert projected["bridge_plan"]["max_attempts"] == 20
    assert projected["bridge_plan"]["max_visible_replies"] == 18
    assert projected["live_text_window"]["max_model_attempts"] == 21
    assert projected["live_text_window"]["max_send_attempts"] == 18
    assert projected["live_text_window"]["max_input_chars"] == 1000
    assert projected["live_text_window"]["max_reply_chars"] == 1500
    assert projected["paid_model"] == {"window_calls": 21}
    assert set(projected) == {"bridge_plan", "live_text_window", "paid_model"}
    profile = load_limits(ROOT / "trial-limits.example.toml")
    assert profile.max_inputs == profile.max_model_attempts == profile.max_send_attempts == 15


@pytest.mark.parametrize("field,value", [
    ("max_inputs", 0), ("max_inputs", True), ("max_model_attempts", 1.5),
    ("max_model_attempts", 2**31), ("max_send_attempts", -1),
    ("max_visible_replies", 0), ("max_input_chars", 0), ("max_reply_chars", 20001),
])
def test_profile_rejects_invalid_limits(field, value):
    data = limits().model_dump()
    with pytest.raises(ValidationError):
        TrialLimits.model_validate({**data, field: value})


def test_cli_uses_one_profile_and_does_not_echo_invalid_private_values(tmp_path):
    command = [sys.executable, "-m", "scripts.trial_limits"]
    result = subprocess.run([*command, str(ROOT / "trial-limits.example.toml")], cwd=ROOT,
                            env={**os.environ, "PYTHONUTF8": "1"}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == load_limits(ROOT / "trial-limits.example.toml").project()
    profile = tmp_path / "invalid.toml"
    credential = "sk-" + "synthetic" * 4
    profile.write_text('[trial_limits]\nunknown = "' + credential + '"\n', encoding="utf-8")
    result = subprocess.run([*command, str(profile)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 2 and result.stderr.strip() == "INVALID_TRIAL_LIMITS"
    assert credential not in result.stdout + result.stderr


def test_sixteen_inputs_model_calls_and_sends_use_configured_caps(core, tmp_path):
    async def scenario():
        async with native_trial(core, tmp_path, limits()) as (
            active, _, calls, sends, capture, finish,
        ):
            for number in range(1, 17):
                await capture()
                assert await finish()
                assert active.attempts() == {"ACK": number}
            assert len(calls) == len(sends) == 16
            with pytest.raises(httpx.HTTPStatusError) as error:
                await capture()
            assert error.value.response.status_code == 429
            assert len(calls) == len(sends) == 16
            operations = view(core)["operations"]
            assert sum(k.startswith("input:") for k in operations) == 16
            assert sum(k.startswith("send:") for k in operations) == 16
            assert len(view(core)["models"]) == 16
            assert active.runtime.row()["state"] == "ACTIVE"
    run(scenario())


@pytest.mark.parametrize("caps", [
    {"max_inputs": 2, "max_model_attempts": 6, "max_send_attempts": 6, "max_visible_replies": 6},
    {"max_inputs": 6, "max_model_attempts": 2, "max_send_attempts": 6, "max_visible_replies": 6},
    {"max_inputs": 6, "max_model_attempts": 6, "max_send_attempts": 2, "max_visible_replies": 6},
    {"max_inputs": 6, "max_model_attempts": 6, "max_send_attempts": 6, "max_visible_replies": 2},
])
def test_each_quota_stops_additional_io_independently(core, tmp_path, caps):
    async def scenario():
        async with native_trial(core, tmp_path, TrialLimits(**caps)) as (
            active, _, calls, sends, capture, finish,
        ):
            for _ in (1, 2):
                await capture()
                assert await finish()
            if caps["max_inputs"] == 2:
                with pytest.raises(httpx.HTTPStatusError) as error:
                    await capture()
                assert error.value.response.status_code == 429
                assert len(calls) == 2
            else:
                await capture()
                assert await core.run_once("independent-native-limits")
                if caps["max_model_attempts"] == 2:
                    assert len(calls) == 2
                    failed = core.store.one("SELECT event_id FROM jobs WHERE state='FAILED'")
                    assert_failure_notice(core, failed["event_id"])
                    assert await active.pump()
                    assert active.attempts() == {"ACK": 3}
                else:
                    # Exhausted send quota never invents a coupling to model quota.
                    assert len(calls) == 3
                    assert await active.pump()
                    assert active.runtime.row()["state"] == "STOPPED"
                    assert active.attempts() == {"ACK": 2, "UNKNOWN": 1}
            notice_sent = caps["max_model_attempts"] == 2
            assert len(sends) == 2 + notice_sent
            operations = view(core)["operations"]
            assert sum(k.startswith("input:") for k in operations) == (
                2 if caps["max_inputs"] == 2 else 3)
            assert sum(k.startswith("send:") for k in operations) == 2 + notice_sent
            assert len(view(core)["models"]) == len(calls)
    run(scenario())


def test_core_and_bridge_reject_divergent_send_caps(core, tmp_path):
    cfg, plan = configured(core, tmp_path, limits())
    _, _, inventory, _ = plan_fixture(tmp_path)
    changed = {**plan.value, "live_text": {**plan.value["live_text"], "max_send_attempts": 17}}
    with pytest.raises(ValueError, match="INVALID_WINDOW_SEND_LIMIT"):
        WindowPlan(changed, cfg, inventory)


def test_large_trial_profile_does_not_override_daily_budget(core, tmp_path):
    async def scenario():
        async with native_trial(core, tmp_path, limits(), paid={"daily_calls": 1}) as (
            active, _, calls, sends, capture, finish,
        ):
            await capture()
            assert await finish()
            await capture()
            assert await core.run_once("daily-cap-before-provider")
            failed = core.store.one("SELECT event_id FROM jobs WHERE state='FAILED'")
            assert_failure_notice(core, failed["event_id"])
            assert await active.pump()
            assert len(calls) == 1 and len(sends) == 2
            assert sum(k.startswith("send:") for k in view(core)["operations"]) == 2
            assert len(view(core)["models"]) == 1
    run(scenario())


def test_text_limits_above_old_trial_cap_work_through_core_and_bridge(core, tmp_path):
    text, reply = "合成输入" * 150, "合成回答" * 150

    def respond(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    async def scenario():
        async with native_trial(core, tmp_path, limits(max_input_chars=1000, max_reply_chars=1000),
            texts=[text], respond=respond,
        ) as (_, _, calls, sends, capture, finish):
            await capture()
            assert await finish()
            assert len(calls) == len(sends) == 1
            assert sends[0]["message"][0]["data"]["text"] == reply
    run(scenario())


@pytest.mark.parametrize("turns", [5, 15])
def test_history_capacity_matches_existing_recent_message_configuration(core, tmp_path, turns):
    # Historical numeric schema inspection grants no execution authority.
    cfg, plan = live_fixture(core, tmp_path, core_options={"max_recent_messages": turns * 2},
        bridge_options={"capture_signing_key": "c" * 64},
        history_scope="approved_window", max_history_turns=turns, max_history_utf8_bytes=16000)
    assert core.settings.max_recent_messages == turns * 2
    assert plan.value["live_text"]["max_history_turns"] == turns
    assert not core.store.rows("SELECT * FROM model_usage")


def test_sixth_captured_reply_can_use_five_complete_history_turns(core, tmp_path):
    value = TrialLimits(max_inputs=6, max_model_attempts=6, max_send_attempts=6,
                        max_visible_replies=6)
    texts = [f"第{index}个已批准的合成输入" for index in range(1, 7)]

    async def scenario():
        async with native_trial(core, tmp_path, value, turns=5, texts=texts) as (
            _, _, calls, sends, capture, finish,
        ):
            for _ in texts:
                await capture()
                assert await finish()
            assert len(calls) == len(sends) == 6
            final = calls[-1]["messages"]
            assert len(final) == 12
            assert [message["content"] for message in final if message["role"] == "user"] == texts
    run(scenario())

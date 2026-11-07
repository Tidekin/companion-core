from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

from companion import cumulative
from companion.trial_limits import TrialLimits
from tests.support.cumulative_budget import policy as quota_policy
from tests.support.live_continuity import native_continuity
from tests.support.paid_model import approved_live


def limits(**changes):
    return TrialLimits.model_validate({
        "max_inputs": 16, "max_model_attempts": 21, "max_send_attempts": 20,
        "max_visible_replies": 18, **changes,
    })


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

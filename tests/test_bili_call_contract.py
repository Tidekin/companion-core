"""Bili stage capacity is explicit in the frozen native paid/window contract."""

import pytest

from companion import image_input, live_text
from companion.video_input import call_limit_matches
from tests.conftest import run
from tests.support.onebot_capture import source_window
from tests.test_bili_video import paid_options


@pytest.mark.parametrize("calls", [4, 6, 10])
def test_signed_bili_scope_accepts_configured_call_limit_at_native_window(core, tmp_path, calls):
    options = paid_options(core)
    options["paid_options"]["per_request_calls"] = calls

    async def source(ws):
        await ws.wait_closed()

    async def scenario():
        async with source_window(core, tmp_path, source, live=True, approved=True,
                                 rich_input_scope="references_v1", scope_options=options):
            live_text.guard(core.store, core.settings, core.clock)
            assert call_limit_matches(core.settings, 4)
            image_input.contract(core.settings, "job_synthetic", "job_synthetic")
            assert not core.store.rows("SELECT * FROM model_usage")

    run(scenario())


@pytest.mark.parametrize("mutation", ["disabled", "scope", "policy", "links", "too_low"])
def test_extra_calls_cannot_outlive_the_exact_bili_authorization(core, tmp_path, mutation):
    options = paid_options(core)
    options["paid_options"]["per_request_calls"] = 6

    async def source(ws):
        await ws.wait_closed()

    async def scenario():
        async with source_window(core, tmp_path, source, live=True, approved=True,
                                 rich_input_scope="references_v1", scope_options=options):
            settings = core.settings.model_copy(deep=True)
            if mutation == "disabled":
                settings.bili_video = settings.bili_video.model_copy(update={"enabled": False})
            elif mutation == "too_low":
                settings.paid_model = settings.paid_model.model_copy(
                    update={"per_request_calls": 3})
            else:
                field, value = {
                    "scope": ("bili_video_scope", "none"),
                    "policy": ("bili_video_policy_sha256", "0" * 64),
                    "links": ("link_scope", "none"),
                }[mutation]
                settings.live_text_window = settings.live_text_window.model_copy(
                    update={field: value})
            assert not call_limit_matches(settings, 4)
            assert not core.store.rows("SELECT * FROM model_usage")

    run(scenario())

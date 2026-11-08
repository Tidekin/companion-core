"""Optional routed request windows, original custody and frozen legacy serialization."""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from pydantic import Field, SecretStr, ValidationError

from companion import budget, owner_authority, paid_budget
from companion import token_measurement as tm
from companion.config import PaidModelApproval, Settings
from companion.contracts import CoreError
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.store import encode
from companion.video_input import VideoInputPolicy
from tests.conftest import run
from tests.owner_model_fixtures import authorized_job
from tests.support.native_consumer_entry import environment
from tests.support.paid_image_budget import image_approval, image_messages
from tests.support.paid_model import approved_live
from tests.support.token_measurement import MESSAGES, configure
from tests.support.video_understanding import video_policy


class LegacyVideo(VideoInputPolicy):
    # The pre-field shape, including its original validators and serializer.
    max_context_tokens: ClassVar[None] = None


class LegacyPaid(PaidModelApproval):
    max_context_tokens: ClassVar[None] = None
    video_input: LegacyVideo | None = Field(default=None, exclude_if=lambda value: value is None)


class LegacySettings(Settings):
    paid_model: LegacyPaid | None = None


@pytest.mark.parametrize("value", [True, 0, -1, "1000", 1000001])
def test_context_limit_is_an_optional_strict_positive_integer(core, tmp_path, value):
    approved_live(core, tmp_path)
    with pytest.raises(ValidationError):
        PaidModelApproval.model_validate({**core.settings.paid_model.model_dump(),
                                          "max_context_tokens": value})
    with pytest.raises(ValidationError):
        video_policy(max_context_tokens=value)


@pytest.mark.parametrize("media", ["text", "image", "video"])
def test_none_preserves_old_approval_settings_snapshot_and_policy_bytes(core, tmp_path, media):
    if media == "text":
        approved_live(core, tmp_path)
    else:
        image_approval(core, tmp_path)
    if media == "video":
        core.settings.paid_model = core.settings.paid_model.model_copy(update={
            "video_input": video_policy()})
        core.settings.video_model_key = SecretStr("synthetic-video-context-key")
    original = core.settings
    legacy = LegacySettings.model_validate(original.model_dump(mode="python"))
    explicit = original.model_copy(deep=True)
    explicit.paid_model = explicit.paid_model.model_copy(update={"max_context_tokens": None})
    if explicit.paid_model.video_input:
        explicit.paid_model = explicit.paid_model.model_copy(update={"video_input":
            explicit.paid_model.video_input.model_copy(update={"max_context_tokens": None})})
    for mode in ("python", "json"):
        assert legacy.model_dump(mode=mode) == explicit.model_dump(mode=mode)
        assert legacy.paid_model.model_dump(mode=mode) == explicit.paid_model.model_dump(mode=mode)
    assert legacy.model_dump_json() == explicit.model_dump_json()
    assert legacy.paid_model.model_dump_json() == explicit.paid_model.model_dump_json()
    assert encode(paid_budget.snapshot(legacy)) == encode(paid_budget.snapshot(explicit))
    assert owner_authority.capability_hashes(legacy) == owner_authority.capability_hashes(explicit)
    policy = OwnerGrantPolicy(owner_qq_id=original.owner_qq_id, purposes=["evaluation"],
        authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(legacy)))
    assert encode(policy.model_dump(mode="json")) == encode(policy.model_copy(update={
        "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(explicit))}
        ).model_dump(mode="json"))


SCENARIOS = ["chat", "memory_extract", "memory_review", "diary", "reflection", "activity",
             "social_autonomy", "qq_poke", "evaluation", "rem", "meme_caption", "content_vision",
             "content_video", "content_video_preview"]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_joint_boundary_uses_the_actual_scenario_route(core, tmp_path, scenario):
    approved_live(core, tmp_path, max_output_tokens=8192, video_input=video_policy())
    output = core.settings.output_tokens(scenario)
    measured = tm.Measurement(payload_sha256="a" * 64, provider="synthetic", endpoint="synthetic",
        model="synthetic", count_kind="estimate", input_tokens=100, reservation_margin=25,
        output_allowance=output, method="synthetic")
    field = "video_input" if scenario in {"content_video", "content_video_preview"} else None
    approval = core.settings.paid_model
    route = approval.video_input if field else approval
    for delta in (0, -1):
        changed = route.model_copy(update={"max_context_tokens": 125 + output + delta})
        selected = approval.model_copy(update={"video_input": changed}) if field else changed
        if delta == 0:
            budget.check_request_limits(selected, measured, scenario)
        else:
            with pytest.raises(CoreError, match="MODEL_CONTEXT_LIMIT"):
                budget.check_request_limits(selected, measured, scenario)
    if field:
        # The chat route's window is unrelated to the separately approved video model.
        budget.check_request_limits(approval.model_copy(update={"max_context_tokens": 1}),
                                    measured, scenario)


def test_joint_refusal_precedes_paid_reservation_and_preserves_tools(core, tmp_path):
    from tests.support.token_tools import CONTINUATION, TOOLS

    configure(core, tmp_path)
    measured = tm.measure_request(core.settings, CONTINUATION, tools=TOOLS)
    core.settings.paid_model = core.settings.paid_model.model_copy(update={
        "max_context_tokens": measured.reserved_input_tokens + measured.output_allowance - 1})
    job_id = authorized_job(core)
    before = core.store.rows("SELECT * FROM model_usage")
    with pytest.raises(CoreError, match="MODEL_CONTEXT_LIMIT"):
        budget.reserve(core.store, core.settings, job_id, "chat", CONTINUATION, core.clock(),
                       clock=core.clock, tools=TOOLS)
    assert core.store.rows("SELECT * FROM model_usage") == before == []
    assert core.store.rows("SELECT * FROM model_admissions") == []


def test_limits_are_rechecked_after_the_real_http_request_hook(core, tmp_path, monkeypatch):
    from companion.model import ModelClient

    configure(core, tmp_path)
    measured = tm.measure_request(core.settings, MESSAGES)
    core.settings.paid_model = core.settings.paid_model.model_copy(update={
        "max_context_tokens": measured.reserved_input_tokens + measured.output_allowance})
    job_id = authorized_job(core)
    checks, dispatched = [], []
    hooked = False
    original = budget.check_request_limits

    def limits(approval, measurement, scenario):
        checks.append(hooked)
        original(approval, measurement, scenario)

    monkeypatch.setattr(budget, "check_request_limits", limits)
    request_id = budget.reserve(core.store, core.settings, job_id, "chat", MESSAGES, core.clock(),
                                clock=core.clock)

    async def hook(request):
        nonlocal hooked
        hooked = True

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            dispatched.append(json.loads(request.content)) or httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1}})),
            event_hooks={"request": [hook]}) as client:
            await budget.complete(ModelClient(core.settings, client), core.store, core.settings,
                                  request_id, MESSAGES, "chat", core.clock)

    run(exercise())
    assert checks == [False, False, True]
    assert len(dispatched) == 1 and dispatched[0]["max_tokens"] == measured.output_allowance


def test_non_none_limit_cannot_inherit_an_old_owner_grant(core, tmp_path):
    configure(core, tmp_path)
    job_id = authorized_job(core)
    core.settings.paid_model = core.settings.paid_model.model_copy(
        update={"max_context_tokens": 99999})
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_PAID_POLICY_CHANGED"):
        budget.reserve(core.store, core.settings, job_id, "chat", MESSAGES, core.clock(),
                       clock=core.clock)
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_explicit_none_retains_the_original_journal_and_admission_contract(core, tmp_path):
    from companion.model import ModelClient

    configure(core, tmp_path)
    core.settings.paid_model = LegacyPaid.model_validate(core.settings.paid_model.model_dump())
    job_id = authorized_job(core)
    request_id = budget.reserve(core.store, core.settings, job_id, "chat", MESSAGES, core.clock(),
                                clock=core.clock)
    saved = core.store.one('SELECT snapshot_json FROM model_admissions WHERE request_id=%s',
                           (request_id,))["snapshot_json"]
    journal = Path(core.settings.paid_model.journal_path)
    original = journal.read_bytes()
    core.settings.paid_model = PaidModelApproval.model_validate({
        **core.settings.paid_model.model_dump(), "max_context_tokens": None})
    with core.store.transaction():
        paid_budget.journal(core.store, core.settings.paid_model)
    assert journal.read_bytes() == original

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1}}))) as client:
            await budget.complete(ModelClient(core.settings, client), core.store, core.settings,
                                  request_id, MESSAGES, "chat", core.clock)

    run(exercise())
    assert core.store.one('SELECT snapshot_json FROM model_admissions WHERE request_id=%s',
                          (request_id,))["snapshot_json"] == saved
    assert "max_context_tokens" not in saved


def test_authorized_media_keeps_estimate_identity_and_output_reserve(core, tmp_path):
    image_approval(core, tmp_path, max_context_tokens=10000)
    measured = tm.measure_request(core.settings, image_messages(), "content_vision")
    assert measured.count_kind == "estimate"
    budget.check_request_limits(core.settings.paid_model, measured, "content_vision")
    assert measured.output_allowance == 300
    assert core.settings.token_measurement is None


@pytest.mark.parametrize("allowed", [False, True])
def test_signed_http_ingress_to_independent_worker_enforces_loaded_joint_window(
    core, tmp_path, allowed,
):
    configure(core, tmp_path)

    async def exercise():
        async with environment(core, tmp_path, purposes=("chat",), paid_options={
            "provider": "deepseek", "model": "deepseek-flash", "endpoint": "https://api.deepseek.com",
            "max_context_tokens": 20000 if allowed else 1,
        }) as (entry, _):
            source = await entry.admit(1, "synthetic joint-window source")
            job_id = entry.accepted[source["event_id"]]["job_id"]
            settings = core.settings.model_dump(mode="json")
            for key in ("database", "model_key", "bridge_token", "control_token"):
                settings[key] = getattr(core.settings, key).get_secret_value()
            path = tmp_path / "context-window-worker.json"
            path.write_text(encode({"settings": settings, "now": core.clock(), "job_id": job_id}),
                            encoding="utf-8")
            process = await asyncio.create_subprocess_exec(sys.executable, "-B",
                str(Path(__file__).with_name("context_window_worker.py")), str(path),
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 35)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
            assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
            assert json.loads(stdout) == {"mock_provider_requests": int(allowed)}
            row = core.store.one((
                'SELECT state,last_error_code FROM jobs WHERE job_id=%s'
            ), (job_id,))
            assert row["state"] == ("SUCCEEDED" if allowed else "FAILED"), row
            if not allowed:
                assert row["last_error_code"] == "MODEL_CONTEXT_LIMIT"
                assert not core.store.rows("SELECT * FROM model_usage")

    asyncio.run(exercise())

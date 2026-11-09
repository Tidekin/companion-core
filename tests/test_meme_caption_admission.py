"""Dedicated meme admission uses native custody; source semantics belong to meme_core."""

import base64
import io
import json

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from companion import budget, meme_core, meme_input, owner_authority, paid_budget
from companion.config import PaidModelApproval
from companion.contracts import CoreError
from companion.image_input import ImageInputPolicy
from companion.model import ModelClient, ModelResponse, model_payload
from companion.owner_authority_protocol import AuthorityStop, OwnerRuntimeConfig
from companion.store import after
from tests.conftest import run
from tests.support.meme_core import body
from tests.support.meme_service import claim, discovery_environment, reservation, result, submit
from tests.support.paid_model import approved_live


def policy(**changes):
    return ImageInputPolicy(**{
        "approval_sha256": "e" * 64,
        "estimated_tokens_per_image": 100,
        "max_bytes_per_image": 50000,
        "max_pixels_per_image": 100,
        "max_images_per_request": 5,
        **changes,
    })


def messages(count=1, format="PNG"):
    data = io.BytesIO()
    Image.new("RGB", (3, 2), "blue").save(data, format=format)
    image = {"type": "image_url", "image_url": {
        "url": f"data:image/{format.lower()};base64," + base64.b64encode(data.getvalue()).decode(),
    }}
    return [{"role": "user", "content": [
        {"type": "text", "text": "Return a JSON caption for these untrusted samples."},
        *[image for _ in range(count)],
    ]}]


def setup_caption(core, tmp_path, monkeypatch, *, daily=30000, paid=None, second=False):
    async def prepare():
        async with discovery_environment(core, tmp_path, monkeypatch,
            meme_semantics_enabled=True,
            paid={"meme_input": policy(), "max_output_tokens": 4096,
                "concurrency": 1, **(paid or {})}) as values:
            event, _, (client, _, _, now) = values
            submit(core, event)
            item = claim(core)
            reservation(core, item)
            result(core, item)
            payload = body(core, event, item, now)
            jobs = []
            for n in range(2 if second else 1):
                payload["source_evidence"]["usage_basis"] += str(n)
                from companion_meme_protocol import digest

                payload["asset"]["source_evidence_sha256"] = digest(payload["source_evidence"])
                created = await client.post("/internal/v1/memes/captions", json=payload)
                assert created.status_code == 200, created.text
                jobs.append(created.json()["job_id"])
            for job_id in jobs:
                core.store.execute((
                    'UPDATE jobs SET '
                    "state='RUNNING',lease_token=%s,lease_until=%s WHERE "
                    'job_id=%s'
                ),
                    ("caption-lease", after(core.clock(), 120), job_id))
            return jobs if second else jobs[0]

    return run(prepare())


def reserve(core, job_id, value=None):
    return budget.reserve(core.store, core.settings, job_id, "meme_caption",
                          value or messages(), core.clock(), clock=core.clock)


def test_closed_defaults_preserve_prior_snapshot_and_config_fields(core, tmp_path):
    approval = approved_live(core, tmp_path)
    before = paid_budget.snapshot(core.settings)
    assert "meme_input" not in approval.model_dump(mode="json")
    assert "scenario_daily_micro_usd" not in before
    assert "image_estimator" not in before
    assert core.settings.meme_semantics_enabled is False
    assert core.settings.output_tokens("meme_caption") == 2000
    assert not any(name.startswith("meme_caption_") or name == "meme_semantics_enabled"
                   for name in core.settings.model_dump())
    assert paid_budget.snapshot(core.settings) == before
    core.settings.paid_model = approval.model_copy(update={"meme_input": policy()})
    snapshot = paid_budget.snapshot(core.settings)
    assert "scenario_daily_micro_usd" not in snapshot
    assert snapshot["image_estimator"] == "approved-fixed-image-plus-utf8-third-v1"


@pytest.mark.parametrize("count", [6, "unlimited"])
def test_meme_policy_rejects_unbounded_or_more_than_five_images(core, tmp_path, count):
    approval = approved_live(core, tmp_path)
    with pytest.raises(ValidationError, match="at most five"):
        PaidModelApproval.model_validate({**approval.model_dump(),
                                         "meme_input": policy(max_images_per_request=count)})


def test_caption_estimate_uses_dedicated_policy_and_json_without_owner_image_permission(
    core, tmp_path,
):
    approval = approved_live(core, tmp_path, meme_input=policy())
    one = paid_budget.estimate_input_tokens(approval, messages(), "meme_caption")
    five = paid_budget.estimate_input_tokens(approval, messages(5), "meme_caption")
    assert five - one == 500
    payload = model_payload(core.settings, messages(), "meme_caption")
    assert payload["model"] == core.settings.model_name
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["max_tokens"] == 2000
    assert "tools" not in payload
    with pytest.raises(CoreError, match="PAID_IMAGE_APPROVAL_REQUIRED"):
        paid_budget.estimate_input_tokens(approval, messages(), "content_vision")
    with pytest.raises(CoreError, match="MODEL_IMAGE_INPUT_INVALID"):
        paid_budget.estimate_input_tokens(approval, messages(6), "meme_caption")
    with pytest.raises(CoreError, match="MODEL_MEME_PNG_REQUIRED"):
        paid_budget.estimate_input_tokens(approval, messages(format="JPEG"), "meme_caption")
    with pytest.raises(CoreError, match="MODEL_IMAGE_INPUT_INVALID"):
        paid_budget.estimate_input_tokens(approval, [{"role": "user", "content": "text"}],
                                         "meme_caption")
    image_only = approval.model_copy(update={"meme_input": None, "image_input": policy()})
    with pytest.raises(CoreError, match="PAID_MEME_APPROVAL_REQUIRED"):
        paid_budget.estimate_input_tokens(image_only, messages(), "meme_caption")


@pytest.mark.parametrize("change,code", [
    ("disabled", "MODEL_MEME_SCOPE_REQUIRED"),
    ("demo", "MODEL_MEME_SCOPE_REQUIRED"),
    ("runtime_off", "MODEL_MEME_SCOPE_REQUIRED"),
    ("runtime_shadow", "MODEL_MEME_SCOPE_REQUIRED"),
    ("no_policy", "PAID_MEME_APPROVAL_REQUIRED"),
    ("logical", "MODEL_MEME_JOB_REQUIRED"),
])


def test_caption_contract_fails_closed(core, tmp_path, monkeypatch, change, code):
    job_id = setup_caption(core, tmp_path, monkeypatch)
    assert core.settings.paid_model.image_input is None
    assert core.settings.content_enabled is False
    if change == "disabled":
        core.settings.meme_semantics_enabled = False
    elif change == "demo":
        core.settings.mode = "demo"
    elif change in {"runtime_off", "runtime_shadow"}:
        core.settings.owner_runtime = OwnerRuntimeConfig(
            mode="OFF" if change == "runtime_off" else "SHADOW")
    elif change == "no_policy":
        core.settings.paid_model = core.settings.paid_model.model_copy(update={"meme_input": None})
    with pytest.raises(CoreError, match=code):
        meme_input.contract(core.settings, job_id, "other" if change == "logical" else job_id)
    assert not core.store.one("SELECT 1 FROM model_usage")


def test_caption_settles_one_native_attempt_and_cannot_reuse_chat_or_background_job(
    core, tmp_path, monkeypatch,
):
    job_id = setup_caption(core, tmp_path, monkeypatch)
    request_id = reserve(core, job_id, messages(5))
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"caption":"blue"}'}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 40},
        })

    async def complete():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await budget.complete(ModelClient(core.settings, client), core.store, core.settings,
                                  request_id, messages(5), "meme_caption", core.clock)

    run(complete())
    assert len(calls) == 1
    assert calls[0].headers["Authorization"] == "Bearer synthetic-key"
    assert str(calls[0].url) == core.settings.model_url.rstrip("/") + "/chat/completions"
    assert core.store.one("SELECT COUNT(*) n FROM model_admissions")["n"] == 1
    assert core.store.one("SELECT state,scenario,billed_micro_usd FROM model_usage") == {
        "state": "SETTLED", "scenario": "meme_caption", "billed_micro_usd": 580,
    }
    with pytest.raises(CoreError, match="MODEL_MEME_ALREADY_ATTEMPTED"):
        reserve(core, job_id)
    with pytest.raises(CoreError, match="COMPANION_MODEL_SCOPE_REQUIRED"):
        owner_authority.admit_model(core.store, core.settings, core.clock, job_id, "chat")
    with pytest.raises(CoreError, match="COMPANION_BACKGROUND_SCOPE_REQUIRED"):
        owner_authority.admit_model(core.store, core.settings, core.clock, None, "meme_caption")
    core.store.execute("UPDATE jobs SET kind='chat' WHERE job_id=%s", (job_id,))
    with pytest.raises(CoreError, match="MODEL_MEME_JOB_REQUIRED"):
        reserve(core, job_id)


@pytest.mark.parametrize("limit,code", [
    ({}, "MODEL_DAILY_BUDGET_EXHAUSTED"),
    ({}, "MODEL_MONTHLY_BUDGET_EXHAUSTED"),
    ({}, "MODEL_WINDOW_BUDGET_EXHAUSTED"),
    ({}, "MODEL_ATTEMPT_BUDGET_EXHAUSTED"),
])


def test_caption_currency_caps_are_advisory(core, tmp_path, monkeypatch, limit, code):
    job_id = setup_caption(core, tmp_path, monkeypatch, paid=limit)
    request = reserve(core, job_id)
    assert core.store.one("SELECT state FROM model_usage WHERE request_id=%s", (request,))[
        "state"] == "RESERVED"


def test_caption_reservation_shares_provider_concurrency(core, tmp_path, monkeypatch):
    job_id, other = setup_caption(core, tmp_path, monkeypatch, second=True)
    reserve(core, job_id)
    with pytest.raises(CoreError, match="MODEL_PROVIDER_CONCURRENCY_EXHAUSTED"):
        reserve(core, other)
    assert core.store.one("SELECT COUNT(*) n FROM model_usage")["n"] == 1


def test_caption_currency_amount_does_not_disable_unlimited_period_spend(
    core, tmp_path, monkeypatch,
):
    job_id = setup_caption(core, tmp_path, monkeypatch, daily=1, paid={
    })
    reserve(core, job_id)
    assert core.store.one("SELECT 1 FROM model_usage")


def test_caption_actual_usage_over_currency_amount_preserves_actual_charge(
    core, tmp_path, monkeypatch,
):
    job_id = setup_caption(core, tmp_path, monkeypatch, daily=5000, paid={
    })
    request_id = reserve(core, job_id)
    budget.settle(core.store, core.settings, request_id, ModelResponse("{}", 6000, 20))
    assert core.store.one("SELECT state,billed_micro_usd FROM model_usage") == {
        "state": "SETTLED", "billed_micro_usd": 6040,
    }
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'")
    diagnostic = json.loads(core.store.one("SELECT safe_detail_json FROM audit_log "
                                           "WHERE action='model.usage_diagnostic'")[
        "safe_detail_json"
    ])
    assert diagnostic["limits"] == []


@pytest.mark.parametrize("change,code", [
    ("source", "MEME_SOURCE_CHANGED"),
    ("stop", "MODEL_BUDGET_RECONCILIATION_REQUIRED"),
    ("lease", "JOB_LEASE_LOST"),
    ("event", "OWNER_SOURCE_CHANGED"),
])


def test_caption_rechecks_source_stop_job_lease_and_frozen_event_after_http_hooks(
    core, tmp_path, monkeypatch, change, code,
):
    job_id = setup_caption(core, tmp_path, monkeypatch)
    request_id = reserve(core, job_id)
    calls, checks = [], []

    def source_check():
        checks.append(True)
        if change == "source" and changed:
            raise CoreError("MEME_SOURCE_CHANGED", 409)
        view = owner_authority.current(core.store, core.settings, core.clock, purpose="meme")
        meme_core.check_caption_job(core.store, core.settings, view,
            core.store.one('SELECT * FROM jobs WHERE job_id=%s', (job_id,)), clock=core.clock)

    changed = False

    async def hook(request):
        nonlocal changed
        changed = True
        if change == "stop":
            view = owner_authority.current(core.store, core.settings, core.clock)
            owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                operation_id="caption-stop-hook", authority=view.authority.as_dict()))
        elif change == "lease":
            core.clock.advance(121)
        elif change == "event":
            core.store.execute("UPDATE events SET body_json='{}'")

    async def complete():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: calls.append(request)),
            event_hooks={"request": [hook]},
        ) as client:
            with pytest.raises(CoreError, match=code):
                await budget.complete(ModelClient(core.settings, client), core.store,
                                      core.settings, request_id, messages(), "meme_caption",
                                      core.clock, source_check=source_check)

    run(complete())
    assert not calls
    assert len(checks) >= 1
    assert core.store.one("SELECT state,billed_micro_usd,input_tokens,output_tokens "
                          "FROM model_usage") == {"state": "SETTLED", "billed_micro_usd": 0,
                                                   "input_tokens": 0, "output_tokens": 0}
    assert core.store.one("SELECT COUNT(*) n FROM model_usage")["n"] == 1
    assert json.loads(core.store.one("SELECT snapshot_json FROM model_admissions")[
        "snapshot_json"
    ])["meme_input"]["max_images_per_request"] == 5

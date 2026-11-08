"""Synthetic images and Mock HTTP only: no provider or owner data."""

import json
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError

from companion import budget, paid_budget
from companion.contracts import CoreError
from companion.model import ModelClient, ModelResponseError, model_payload
from companion.store import Store
from tests.conftest import run
from tests.owner_model_fixtures import authorized_job
from tests.support.paid_image_budget import image_approval, image_messages, reserve_image
from tests.support.paid_model import approved_live


def provider_body(**changes):
    return {
        "choices": [{"message": {"content": "这是一张合成白色图片。"}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 20, "prompt_cache_hit_tokens": 50},
        **changes,
    }


async def invoke(core, request, messages, *, scenario="content_vision", handler=None, hooks=None):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            handler or (lambda _: httpx.Response(200, json=provider_body()))
        ),
        event_hooks={"request": hooks or []},
    ) as client:
        response = await budget.complete(
            ModelClient(core.settings, client),
            core.store,
            core.settings,
            request,
            messages,
            scenario,
            core.clock,
        )
        budget.settle(core.store, core.settings, request, response)
        return response


def test_native_image_uses_the_explicit_request_call_limit(core, tmp_path):
    image_approval(core, tmp_path, per_request_calls=3)
    job, request = reserve_image(core)
    run(invoke(core, request, image_messages()))
    text = [{"role": "user", "content": "synthetic follow-up"}]
    for _ in range(2):
        request = budget.reserve(core.store, core.settings, job, "chat", text, core.clock())
        run(invoke(core, request, text, scenario="chat"))
    with pytest.raises(CoreError, match="MODEL_REQUEST_BUDGET_EXHAUSTED"):
        budget.reserve(core.store, core.settings, job, "chat", text, core.clock())
    assert core.store.one("SELECT COUNT(*) n FROM model_usage WHERE state='SETTLED'")["n"] == 3


def test_text_snapshot_omits_new_policy_and_keeps_original_shape(core, tmp_path):
    approval = approved_live(core, tmp_path)
    snapshot = paid_budget.snapshot(core.settings)
    assert "image_input" not in snapshot
    assert "content_vision" not in snapshot["scenario_daily_micro_usd"]
    assert snapshot["estimator"] == "utf8-third-v1"
    # Golden pin of this pre-image fixture's canonical text-only contract.
    assert paid_budget.sha(snapshot) == (
        "582e96c6200b6bf1a8d5da93400aadb477228a72ef6561951bac391edf3083c1"
    )
    assert set(snapshot) == set(approval.model_dump()) - {"journal_path", "image_input"} | {
        "estimator",
        "local_monthly_micro_usd",
        "scenario_daily_micro_usd",
        "reflection_monthly_micro_usd",
        "credential_tag",
    }


def test_vision_without_separate_image_approval_creates_no_usage(core, tmp_path):
    approved_live(core, tmp_path, per_request_calls=2)
    core.settings.content.use_chat_model_for_vision = True
    with pytest.raises(CoreError, match="IMAGE_APPROVAL_REQUIRED"):
        reserve_image(core)
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_image_estimate_uses_policy_not_base64_text_and_binds_payload(core, tmp_path):
    approval = image_approval(core, tmp_path)
    messages = image_messages()
    job, request = reserve_image(core, messages)
    usage = core.store.one('SELECT * FROM model_usage WHERE request_id=%s', (request,))
    expected = 32 + 8 + (len(messages[0]["content"][0]["text"].encode()) + 2) // 3 + 1000
    expected = (expected * approval.input_estimate_safety_percent + 99) // 100
    assert usage["input_tokens"] == expected
    assert usage["output_tokens"] == 300
    admission = core.store.one('SELECT * FROM model_admissions WHERE request_id=%s', (request,))
    assert admission["logical_request_id"] == job
    assert admission["payload_sha256"]
    assert paid_budget.snapshot(core.settings)["image_input"] == approval.image_input.model_dump()


@pytest.mark.parametrize("policy", [{"max_pixels_per_image": 3}, {"max_bytes_per_image": 16}])
def test_image_policy_bounds_reject_before_reservation(core, tmp_path, policy):
    image_approval(core, tmp_path, policy=policy)
    with pytest.raises(CoreError, match="IMAGE_INPUT"):
        reserve_image(core)
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_exact_image_and_chat_share_job_journal_usage_and_two_attempt_limit(core, tmp_path):
    image_approval(core, tmp_path)
    messages, calls = image_messages(), []
    job, request = reserve_image(core, messages)

    def handler(req):
        calls.append((str(req.url), req.headers["Authorization"], json.loads(req.content)))
        return httpx.Response(200, json=provider_body())

    run(invoke(core, request, messages, handler=handler))
    text = [{"role": "user", "content": "根据图片观察回复当前问题。"}]
    chat = budget.reserve(core.store, core.settings, job, "chat", text, core.clock())
    run(invoke(core, chat, text, scenario="chat", handler=handler))
    assert calls[0] == (
        core.settings.model_url + "/chat/completions",
        "Bearer synthetic-key",
        model_payload(core.settings, messages, "content_vision"),
    )
    assert calls[1][2] == model_payload(core.settings, text, "chat")
    admissions = core.store.rows('SELECT * FROM model_admissions ORDER BY record_sequence')
    assert {a["logical_request_id"] for a in admissions} == {job}
    assert len({(a["window_id"], a["journal_id"], a["journal_path"]) for a in admissions}) == 1
    assert [a["attempt_number"] for a in admissions] == [1, 2]
    assert core.store.rows(

            'SELECT '
            'scenario,state,input_tokens,output_tokens,cached_input_tokens '
            'FROM model_usage ORDER BY record_sequence'

    ) == [
        {
            "scenario": s,
            "state": "SETTLED",
            "input_tokens": 1000,
            "output_tokens": 20,
            "cached_input_tokens": 50,
        }
        for s in ("content_vision", "chat")
    ]
    with pytest.raises(CoreError, match="REQUEST_BUDGET"):
        budget.reserve(core.store, core.settings, job, "chat", text, core.clock())
    with pytest.raises(CoreError, match="IMAGE_ALREADY_ATTEMPTED"):
        budget.reserve(core.store, core.settings, job, "content_vision", messages, core.clock())
    assert len(calls) == 2


@pytest.mark.parametrize(
    "mutation",
    [
        "remote",
        "second_image",
        "extra_message",
        "detail",
        "wrong_mime",
        "invalid_image",
        "noncanonical_base64",
        "text_only",
        "wrong_role",
    ],
)
def test_unsupported_multimodal_shapes_cannot_create_admission(core, tmp_path, mutation):
    image_approval(core, tmp_path)
    messages = image_messages()
    parts = messages[0]["content"]
    if mutation == "remote":
        parts[1]["image_url"]["url"] = "https://synthetic.invalid/image.png"
    elif mutation == "second_image":
        parts.append(deepcopy(parts[1]))
    elif mutation == "extra_message":
        messages.insert(0, {"role": "system", "content": "instruction"})
    elif mutation == "detail":
        parts[1]["image_url"]["detail"] = "high"
    elif mutation == "wrong_mime":
        parts[1]["image_url"]["url"] = parts[1]["image_url"]["url"].replace("png", "jpeg")
    elif mutation == "invalid_image":
        parts[1]["image_url"]["url"] = "data:image/png;base64,eA=="
    elif mutation == "noncanonical_base64":
        parts[1]["image_url"]["url"] += "="
    elif mutation == "text_only":
        messages[0]["content"] = "caption"
    else:
        messages[0]["role"] = "assistant"
    with pytest.raises(CoreError, match="IMAGE_INPUT_INVALID"):
        reserve_image(core, messages)
    assert core.store.rows("SELECT * FROM model_usage") == []
    assert core.store.rows("SELECT * FROM model_admissions") == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_images_per_request", 0),
        ("max_images_per_request", True),
        ("approval_sha256", ""),
        ("estimated_tokens_per_image", 0),
        ("max_pixels_per_image", 16000001),
        ("max_bytes_per_image", 4000001),
    ],
)
def test_image_approval_is_explicit_and_strict(core, tmp_path, field, value):
    with pytest.raises(ValidationError):
        image_approval(core, tmp_path, policy={field: value})


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("separate_provider", "CONTRACT_MISMATCH"),
        ("no_job", "CHAT_JOB_REQUIRED"),
        ("other_logical", "CHAT_JOB_REQUIRED"),
        ("missing_job", "OWNER_AUTHORITY_JOB_BINDING_REQUIRED"),
    ],
)
def test_image_approval_never_authorizes_another_provider_or_job(core, tmp_path, mutation, code):
    image_approval(core, tmp_path)
    job = authorized_job(core, purposes=("chat", "image"))
    logical = job
    if mutation == "separate_provider":
        core.settings.provider = "unapproved-image-provider"
    elif mutation == "no_job":
        job = logical = None
    elif mutation == "other_logical":
        logical = "arbitrary-logical"
    elif mutation == "missing_job":
        job = logical = "job_missing"
    with pytest.raises(CoreError, match=code):
        budget.reserve(
            core.store,
            core.settings,
            job,
            "content_vision",
            image_messages(),
            core.clock(),
            logical_request_id=logical,
        )
    assert core.store.rows("SELECT * FROM model_usage") == []


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("image", "REQUEST_CHANGED"),
        ("payload", "PAYLOAD_CHANGED"),
        ("policy", "OWNER_AUTHORITY_CAPABILITY_CHANGED"),
        ("provider", "CONTRACT_MISMATCH"),
    ],
)
def test_final_http_hook_drift_blocks_image_dispatch(core, tmp_path, mutation, code):
    image_approval(core, tmp_path)
    messages, calls = image_messages(), []
    _, request = reserve_image(core, messages)

    async def change(req):
        if mutation == "image":
            body = json.loads(req.content)
            body["messages"] = image_messages((3, 3))
            req._content = json.dumps(body).encode()
        elif mutation == "payload":
            messages[0]["content"][0]["text"] += " changed"
        elif mutation == "policy":
            original = core.settings.paid_model
            policy = original.image_input.model_copy(update={"estimated_tokens_per_image": 2000})
            core.settings.paid_model = original.model_copy(update={"image_input": policy})
        else:
            core.settings.provider = "unapproved-image-provider"

    with pytest.raises(CoreError, match=code):
        run(invoke(core, request, messages, hooks=[change], handler=lambda req: calls.append(req)))
    budget.settle(core.store, core.settings, request)
    assert calls == []
    usage = core.store.one("SELECT state,billed_micro_usd FROM model_usage")
    assert usage == {"state": "SETTLED", "billed_micro_usd": 0}
    assert core.store.one("SELECT 1 FROM audit_log WHERE action='model.not_dispatched'")


def test_multimodal_stream_cannot_disagree_with_frozen_json(core, tmp_path):
    image_approval(core, tmp_path)
    messages, received = image_messages(), []
    _, request = reserve_image(core, messages)

    async def change(req):
        req.stream = httpx.ByteStream(b'{"unapproved":"image"}')

    def handler(req):
        received.append(json.loads(req.read()))
        return httpx.Response(200, json=provider_body())

    run(invoke(core, request, messages, hooks=[change], handler=handler))
    assert received == [model_payload(core.settings, messages, "content_vision")]


@pytest.mark.parametrize("usage", [None, {"prompt_tokens": "1000", "completion_tokens": 20}])
def test_missing_or_invalid_image_usage_retains_amount_and_blocks_retry(core, tmp_path, usage):
    image_approval(core, tmp_path, concurrency=1)
    messages = image_messages()
    job, request = reserve_image(core, messages)
    reserved = core.store.one("SELECT billed_micro_usd FROM model_usage")["billed_micro_usd"]
    run(
        invoke(
            core,
            request,
            messages,
            handler=lambda _: httpx.Response(
                200,
                json=provider_body(usage=usage),
            ),
        )
    )
    assert core.store.one("SELECT state,billed_micro_usd FROM model_usage") == {
        "state": "UNKNOWN",
        "billed_micro_usd": reserved,
    }
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        budget.reserve(core.store, core.settings, job, "content_vision", messages, core.clock())
    with pytest.raises(CoreError, match="RECONCILIATION_REQUIRED"):
        budget.reserve(
            core.store,
            core.settings,
            job,
            "chat",
            [{"role": "user", "content": "reply"}],
            core.clock(),
        )


def test_invalid_provider_text_still_settles_received_image_usage(core, tmp_path):
    image_approval(core, tmp_path)
    messages = image_messages()
    _, request = reserve_image(core, messages)
    with pytest.raises(ModelResponseError, match="MODEL_INVALID_RESPONSE"):
        run(
            invoke(
                core,
                request,
                messages,
                handler=lambda _: httpx.Response(
                    200,
                    json=provider_body(choices=[]),
                ),
            )
        )
    assert core.store.one("SELECT state,input_tokens,output_tokens FROM model_usage") == {
        "state": "SETTLED",
        "input_tokens": 1000,
        "output_tokens": 20,
    }
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_MODEL_ADMISSION_REQUIRED"):
        run(invoke(core, request, messages))


def test_image_scenario_and_global_five_call_caps_share_existing_accounting(core, tmp_path):
    image_approval(core, tmp_path, window_calls=5)
    for n in range(3):
        job, request = reserve_image(core, number=n + 1)
        run(invoke(core, request, image_messages()))
        if n < 2:
            text = [{"role": "user", "content": "reply"}]
            chat = budget.reserve(core.store, core.settings, job, "chat", text, core.clock())
            run(invoke(core, chat, text, scenario="chat"))
    with pytest.raises(CoreError, match="WINDOW_BUDGET"):
        budget.reserve(core.store, core.settings, job, "chat", text, core.clock())
    assert core.store.one("SELECT COUNT(*) AS n FROM model_admissions")["n"] == 5
    assert core.store.one("SELECT COUNT(*) AS n FROM model_usage WHERE state='SETTLED'")["n"] == 5


def test_image_daily_budget_is_part_of_snapshot_and_admission(core, tmp_path):
    image_approval(core, tmp_path)
    core.settings.content.image_daily_micro_usd = 1
    assert paid_budget.snapshot(core.settings)["scenario_daily_micro_usd"]["content_vision"] == 1
    with pytest.raises(CoreError, match="DAILY_BUDGET"):
        reserve_image(core)
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_image_payload_cannot_bypass_policy_as_text_chat(core, tmp_path):
    approved_live(core, tmp_path)
    with pytest.raises(CoreError, match="PAID_IMAGE_APPROVAL_REQUIRED"):
        budget.reserve(core.store, core.settings, None, "chat", image_messages(), core.clock())
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_image_admission_requires_the_real_model_client(core, tmp_path):
    image_approval(core, tmp_path)
    _, request = reserve_image(core)
    with pytest.raises(CoreError, match="MODEL_ADAPTER_REQUIRED"):
        run(budget.complete(core.model, core.store, core.settings, request, image_messages(),
                            "content_vision", core.clock))
    assert core.model.calls == []
    assert core.store.one("SELECT io_started_at FROM model_admissions")["io_started_at"] is None


@pytest.mark.parametrize("column,value", [
    ("scenario", "chat"), ("job_id", None), ("logical_request_id", "other-logical"),
])
def test_final_image_stage_identity_is_not_mutable(core, tmp_path, column, value):
    image_approval(core, tmp_path)
    messages, calls = image_messages(), []
    _, request = reserve_image(core, messages)

    async def change(req):
        table = "model_admissions" if column == "logical_request_id" else "model_usage"
        core.store.execute(f"UPDATE {table} SET {column}=%s WHERE request_id=%s", (value, request))

    code = ("COMPANION_BACKGROUND_SCOPE_REQUIRED" if column == "job_id"
            else "ADMISSION_LEASE_EXPIRED")
    with pytest.raises(CoreError, match=code):
        run(invoke(core, request, messages, hooks=[change], handler=lambda req: calls.append(req)))
    assert calls == []


def test_image_estimate_is_independent_of_base64_length(core, tmp_path):
    approval = image_approval(core, tmp_path)
    small, bigger = image_messages(), image_messages((50, 50))
    assert len(small[0]["content"][1]["image_url"]["url"]) != len(
        bigger[0]["content"][1]["image_url"]["url"]
    )
    assert paid_budget.estimate_input_tokens(approval, small, "content_vision") == (
        paid_budget.estimate_input_tokens(approval, bigger, "content_vision")
    )


def test_image_unknown_survives_reopen_without_fresh_concurrency_or_attempt(core, tmp_path):
    approval = image_approval(core, tmp_path, concurrency=1)
    messages = image_messages()
    job, request = reserve_image(core, messages)

    def timeout(req):
        raise httpx.ReadTimeout("synthetic timeout", request=req)

    with pytest.raises(CoreError, match="MODEL_RETRYABLE"):
        run(invoke(core, request, messages, handler=timeout))
    budget.settle(core.store, core.settings, request)
    before = core.store.one("SELECT * FROM model_usage")
    old_custody = paid_budget.journal(core.store, approval, allow_settlement=True)
    reopened = Store(core.database, core.settings.state_directory)
    try:
        assert reopened.one("SELECT * FROM model_usage") == before
        assert paid_budget.journal(reopened, approval, allow_settlement=True) == old_custody
        with pytest.raises(CoreError, match="IMAGE_ALREADY_ATTEMPTED"):
            budget.reserve(reopened, core.settings, job, "content_vision", messages, core.clock())
        with pytest.raises(CoreError, match="CONCURRENCY"):
            budget.reserve(reopened, core.settings, job, "chat",
                           [{"role": "user", "content": "reply"}], core.clock())
        assert reopened.one("SELECT state,billed_micro_usd FROM model_usage") == {
            "state": "UNKNOWN", "billed_micro_usd": before["billed_micro_usd"],
        }
    finally:
        reopened.close()


def test_real_image_usage_above_configured_estimate_settles_at_observed_usage(core, tmp_path):
    image_approval(core, tmp_path, policy={"estimated_tokens_per_image": 100})
    _, request = reserve_image(core)
    reserved = core.store.one("SELECT billed_micro_usd FROM model_usage")["billed_micro_usd"]
    run(invoke(core, request, image_messages()))
    usage = core.store.one("SELECT * FROM model_usage")
    assert usage["state"] == "SETTLED" and usage["input_tokens"] == 1000
    assert usage["billed_micro_usd"] == budget.cost(core.settings, 1000, 20, 50) > reserved
    assert usage["is_estimate"] == 1  # Configured monetary price is still not a provider invoice.


def test_http_image_error_with_usage_is_accounted_once(core, tmp_path):
    image_approval(core, tmp_path)
    _, request = reserve_image(core)
    with pytest.raises(ModelResponseError, match="MODEL_RETRYABLE"):
        run(invoke(core, request, image_messages(), handler=lambda _: httpx.Response(
            503, json=provider_body(),
        )))
    assert core.store.one("SELECT state,input_tokens,output_tokens FROM model_usage") == {
        "state": "SETTLED", "input_tokens": 1000, "output_tokens": 20,
    }
    assert core.store.one("SELECT COUNT(*) AS n FROM model_admissions")["n"] == 1


def test_observed_image_amount_exceeding_daily_cap_stops_without_losing_usage(core, tmp_path):
    image_approval(core, tmp_path, policy={"estimated_tokens_per_image": 100})
    core.settings.content.image_daily_micro_usd = 900
    _, request = reserve_image(core)
    run(invoke(core, request, image_messages()))
    usage = core.store.one("SELECT * FROM model_usage")
    assert usage["state"] == "UNKNOWN"
    assert usage["input_tokens"] == 1000 and usage["output_tokens"] == 20
    assert usage["billed_micro_usd"] == budget.cost(core.settings, 1000, 20, 50) > 900
    assert core.store.one("SELECT value FROM runtime_meta WHERE key='model_budget_blocked'")

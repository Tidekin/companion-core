"""Official vision requests retain native measurement and owner-approved cost bounds."""
import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from companion import evaluation, paid_budget, token_measurement
from companion.config import PaidModelApproval
from companion.contracts import CoreError
from companion.model import GenerationOptions, ModelAdmission, ModelClient, model_payload
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from scripts import evaluate_image_understanding as experiment
from tests.support.core_http import real_core_http
from tests.support.owner_authority import activate
from tests.test_evaluation_process import processes
from tests.test_paid_model_gate import approved_live


def official_settings():
    cfg = experiment.planning_settings()
    cfg.provider, cfg.model_name, cfg.model_url = (
        "openai", "gpt-6.1-sol", "https://api.openai.com/v1")
    cfg.input_rate, cfg.cached_input_rate, cfg.output_rate = 2000000, 100000, 10000000
    paid = cfg.paid_model.model_dump()
    paid.update(provider=cfg.provider, model=cfg.model_name, endpoint=cfg.model_url,
                input_rate=cfg.input_rate, cached_input_rate=cfg.cached_input_rate,
                output_rate=cfg.output_rate, per_attempt_micro_usd=100000,
                per_request_micro_usd=200000)
    cfg.paid_model = PaidModelApproval.model_validate(paid)
    return cfg


def test_official_completion_allowance_is_the_measured_and_dispatched_limit():
    cfg = official_settings()
    messages = [{"role": "user", "content": "SYNTHETIC public image description"}]
    payload = model_payload(cfg, messages, "evaluation")
    assert payload["max_completion_tokens"] == cfg.max_output_tokens
    assert "max_tokens" not in payload
    measurement = token_measurement.measure_payload(cfg, payload, "evaluation")
    assert measurement.output_allowance == cfg.max_output_tokens
    seen = []

    async def execute():
        def provider(request):
            seen.append(json.loads(request.content))
            assert request.url == "https://api.openai.com/v1/chat/completions"
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "Synthetic description"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 12}})
        cfg.mode = "live"
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            response = await ModelClient(cfg, client).complete(messages, "evaluation",
                admission=ModelAdmission(paid_budget.sha(payload)))
            assert response.text == "Synthetic description"
    asyncio.run(execute())
    assert seen == [payload]


def test_official_gateway_generation_override_keeps_one_output_allowance():
    cfg = official_settings()
    generation = GenerationOptions(max_tokens=31)
    messages = [{"role": "user", "content": "SYNTHETIC"}]
    payload = model_payload(cfg, messages, "gateway", generation=generation)
    assert payload["max_completion_tokens"] == 31 and "max_tokens" not in payload
    assert token_measurement.measure_payload(
        cfg, payload, "gateway", generation=generation).output_allowance == 31


@pytest.mark.parametrize("provider,endpoint", [
    ("deepseek", "https://api.deepseek.com"),
    ("openai", "https://synthetic-compatible.invalid/v1"),
])
def test_other_provider_wire_contracts_keep_their_existing_field(provider, endpoint):
    cfg = experiment.planning_settings()
    cfg.provider, cfg.model_url = provider, endpoint
    payload = model_payload(cfg, [{"role": "user", "content": "SYNTHETIC"}], "chat")
    assert payload["max_tokens"] == cfg.max_output_tokens
    assert "max_completion_tokens" not in payload


def test_image_evaluation_uses_approved_attempt_cost_and_output_bounds(tmp_path):
    cfg = official_settings()
    cases = [next(c for c in experiment.load_cases() if c["id"] == "coffee")]
    experiment.prepare(tmp_path / "prepared", settings=cfg, subset=cases)
    path = next((tmp_path / "prepared").glob("manifest-*.json"))
    manifest = json.loads(path.read_text(encoding="utf-8"))
    costs = [r["estimated_reservation_micro_usd"] for r in manifest["plan"]["requests"]]
    assert costs and all(5000 < cost <= cfg.paid_model.per_attempt_micro_usd for cost in costs)
    evaluation._plan(cfg, manifest)
    cfg.paid_model = cfg.paid_model.model_copy(update={"per_attempt_micro_usd": 5000})
    with pytest.raises(CoreError):
        evaluation._plan(cfg, manifest)


def test_image_evaluation_output_cannot_exceed_approved_allowance(tmp_path):
    cfg = official_settings()
    cfg.max_output_tokens = cfg.paid_model.max_output_tokens + 1
    cases = [next(c for c in experiment.load_cases() if c["id"] == "coffee")]
    with pytest.raises(CoreError):
        experiment.prepare(tmp_path / "over-output", settings=cfg, subset=cases)


def test_larger_approved_output_keeps_native_frozen_measurement(tmp_path):
    cfg = official_settings()
    cfg.paid_model = cfg.paid_model.model_copy(update={"max_output_tokens": 4096})
    cfg.max_output_tokens = 4096
    cases = [next(c for c in experiment.load_cases() if c["id"] == "coffee")]
    experiment.prepare(tmp_path / "larger-output", settings=cfg, subset=cases)
    path = next((tmp_path / "larger-output").glob("manifest-*.json"))
    manifest = json.loads(path.read_text(encoding="utf-8"))
    evaluation._plan(cfg, manifest)
    assert all(row["measurement"]["output_allowance"] == 4096
               for row in manifest["plan"]["requests"])


def test_evaluation_uses_selected_deepseek_reasoning_without_changing_background_tasks():
    cfg = experiment.planning_settings()
    cfg.chat_thinking = "high"
    messages = [{"role": "user", "content": "SYNTHETIC visual reasoning"}]
    payload = model_payload(cfg, messages, "evaluation")
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"
    assert model_payload(cfg, messages, "memory_extract")["thinking"] == {"type": "disabled"}
    cfg.chat_thinking = "disabled"
    assert model_payload(cfg, messages, "evaluation")["thinking"] == {"type": "disabled"}


def test_official_cached_tokens_are_preserved_for_native_accounting():
    cfg = official_settings()
    cfg.mode = "live"
    messages = [{"role": "user", "content": "SYNTHETIC cached image description"}]

    async def execute():
        def provider(request):
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "Synthetic description"}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 20,
                          "prompt_tokens_details": {"cached_tokens": 80}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            response = await ModelClient(cfg, client).complete(messages, "evaluation",
                admission=ModelAdmission(paid_budget.sha(model_payload(cfg, messages,
                                                                      "evaluation"))))
            assert (response.input_tokens, response.cached_tokens) == (120, 80)
            assert response.cache_usage_present is True
    asyncio.run(execute())


@pytest.mark.parametrize("kind", ["synthetic", "owner_upload"])
def test_official_image_plan_uses_real_api_and_independent_native_worker(core, tmp_path, kind):
    cfg = official_settings()
    approved = cfg.paid_model.model_dump(exclude={"journal_path"})
    approved.update(live_approved=True, starts_at="2026-10-01T00:00:00+08:00",
                    expires_at="2026-12-01T00:00:00+08:00",
                    rates_valid_until="2026-12-01T00:00:00+08:00")
    approved_live(core, tmp_path, **approved)
    core.settings.max_output_tokens = cfg.max_output_tokens
    core.settings.context_soft_tokens = cfg.context_soft_tokens
    core.settings.evaluation_enabled = True
    core.settings.control_token = SecretStr("synthetic-control-" * 3)
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    core.store.insert("conversations", conversation_id="synthetic-official-vision",
        platform="evaluation.synthetic", platform_session_key="synthetic-official-vision",
        scope="private", owner_actor_id="owner", created_at=core.clock())
    activate(core, OwnerGrantPolicy(owner_qq_id=core.settings.owner_qq_id,
        purposes=["evaluation"], authorization_sha256="a" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings))))
    case = (next(c for c in experiment.load_cases() if "initial_wrong" in c)
            if kind == "owner_upload" else next(
                c for c in experiment.load_cases() if c["id"] == "coffee"))
    experiment.prepare(tmp_path / "official-plan", settings=core.settings, subset=[case])
    rows = [row for path in sorted((tmp_path / "official-plan").glob("manifest-*.json"))
            for row in json.loads(path.read_text(encoding="utf-8"))["plan"]["requests"]]
    if kind == "owner_upload":
        rows = [row for row in rows if any(isinstance(message["content"], list)
                for message in row["payload"]["messages"])]
    rows = rows[:6]
    manifest = experiment.manifest(core.settings, rows, False, kind=kind)
    assert all(r["estimated_reservation_micro_usd"] > 5000
               for r in manifest["plan"]["requests"])

    async def execute():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + core.settings.control_token.get_secret_value()},
        ) as client:
            preview = await client.post("/internal/v2/evaluations/preview", json=manifest)
            assert preview.status_code == 200, preview.text
            consent = await client.post("/internal/v2/consents", json={
                **preview.json()["consent_scope"], "duration_seconds": 600})
            assert consent.status_code == 200
            submitted = await client.post("/internal/v2/evaluations", json={
                "manifest": manifest, "consent_id": consent.json()["consent_id"]})
            assert submitted.status_code == 202, submitted.text
            hashes = await processes(core, tmp_path / "official-worker", count=1)
            assert hashes == [r["payload_sha256"] for r in manifest["plan"]["requests"]]
            result = await client.get("/internal/v2/evaluations/" +
                                      submitted.json()["evaluation_id"])
            assert result.json()["state"] == "SUCCEEDED", result.text
            assert all(r["state"] == "SETTLED" for r in result.json()["requests"])
    asyncio.run(execute())
    assert core.store.one("SELECT SUM(billed_micro_usd) n FROM model_usage")["n"] > 50000
    assert core.store.rows("SELECT * FROM outbox") == []

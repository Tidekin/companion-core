"""Offline official artifacts, full request measurement and native admission fences."""

import asyncio
import gzip
import hashlib
import io
import json
import shutil
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from companion import budget, paid_budget
from companion import token_measurement as tm
from companion.contracts import CoreError
from companion.dialogue_history import fit_context
from companion.model import ModelClient, model_payload
from companion.store import encode
from tests.owner_model_fixtures import authorized_job
from tests.support.paid_model import approved_live
from tests.support.token_measurement import MESSAGES, configure


@pytest.mark.parametrize("number", range(1, 6))
def test_official_golden_rendering_and_lossless_tokenization(number):
    # Upstream expected text is independent of this project's measurement wrapper.
    root = Path(__file__).parent / "fixtures" / "deepseek_v41"
    renderer, tokenizer = tm._local_encoder()
    case = json.loads((root / f"test_input_{number}.json").read_text("utf-8"))
    if isinstance(case, list):
        case = {"messages": case}
    if "tools" in case:
        case["messages"][0]["tools"] = case["tools"]
    rendered, _ = renderer.encode_case(case, "chat")
    assert rendered == (root / f"test_output_{number}.txt").read_text("utf-8").rstrip("\n")
    ids = tokenizer.encode(rendered, add_special_tokens=False).ids
    assert tokenizer.decode(ids, skip_special_tokens=False) == rendered


def test_exact_full_request_known_ids_and_separate_margin(core, tmp_path):
    configure(core, tmp_path)
    counted = tm.measure_request(core.settings, MESSAGES)
    assert counted.count_kind == "exact_local_render"
    assert counted.input_tokens == 5  # BOS, User, hello, Assistant, closing thinking token.
    assert counted.reservation_margin == 2
    assert counted.output_allowance == core.settings.max_output_tokens
    assert counted.artifact_revision == tm.REVISION
    assert counted.payload_sha256 == paid_budget.sha(model_payload(core.settings, MESSAGES, "chat"))
    assert "hello" not in encode(counted.receipt())


@pytest.mark.parametrize("text", ["中文与 English", "🙂\n第二行", "<｜User｜>", " "])
def test_full_render_counts_boundaries_and_preserves_input(core, tmp_path, text):
    configure(core, tmp_path)
    messages = [{"role": "system", "content": "规则"},
                {"role": "user", "content": text},
                {"role": "assistant", "content": "答复"},
                {"role": "system", "content": "新规则"},
                {"role": "user", "content": "继续"}]
    original = encode(messages)
    result = tm.measure_request(core.settings, messages)
    assert result.count_kind == "exact_local_render"
    assert result.input_tokens > 5
    assert encode(messages) == original


@pytest.mark.parametrize("scenario", ["memory_extract", "memory_review", "diary", "reflection"])
def test_unverified_structured_output_is_estimate_or_rejected(core, tmp_path, scenario):
    configure(core, tmp_path)
    assert tm.measure_request(core.settings, MESSAGES, scenario).count_kind == "estimate"
    core.settings.token_measurement = core.settings.token_measurement.model_copy(
        update={"policy": "exact_required"})
    measured = tm.measure_request(core.settings, MESSAGES, scenario)
    assert measured.count_kind == "unsupported"
    with pytest.raises(CoreError, match="MODEL_TOKEN_COUNT_UNSUPPORTED"):
        _ = measured.reserved_input_tokens


def test_unknown_alias_is_not_guessed(core, tmp_path):
    configure(core, tmp_path)
    core.settings.model_name = "unknown-alias"
    result = tm.measure_request(core.settings, MESSAGES)
    assert (result.count_kind, result.reason) == ("estimate", "route_mismatch")
    with pytest.raises(ValidationError):
        tm.TokenMeasurementConfig.model_validate({
            **core.settings.token_measurement.model_dump(), "model": "unknown-alias"})


def test_context_and_admission_measure_same_full_payload(core, tmp_path):
    configure(core, tmp_path)
    rules = {"role": "system", "content": "required"}
    references = [{"role": "system", "content": "optional " * 100}]
    turns = [[{"role": "user", "content": "older " * 100},
              {"role": "assistant", "content": "reply " * 100}]]
    required = [rules, *MESSAGES]
    limit = tm.measure_request(core.settings, required).reserved_input_tokens
    fitted = fit_context(rules, references, turns, MESSAGES, limit, settings=core.settings)
    assert fitted == required
    request = budget.reserve(core.store, core.settings, authorized_job(core),
        "chat", fitted, core.clock())
    row = core.store.one("SELECT * FROM model_admissions WHERE request_id=?", (request,))
    assert row["input_bound"] == limit
    assert json.loads(row["measurement_json"]) == (
        tm.measure_request(core.settings, fitted).receipt())


def test_disabled_configuration_preserves_contract_and_legacy_estimate(core, tmp_path):
    approved_live(core, tmp_path)
    snapshot = paid_budget.snapshot(core.settings)
    assert "token_measurement" not in snapshot
    assert "token_measurement" not in core.settings.model_dump(mode="json")
    measured = tm.measure_request(core.settings, MESSAGES)
    assert measured.count_kind == "estimate"
    assert measured.reserved_input_tokens == paid_budget.estimate_input_tokens(
        core.settings.paid_model, MESSAGES, "chat")


def test_artifact_corruption_fails_before_native_reservation(core, tmp_path, monkeypatch):
    configure(core, tmp_path)
    tm._local_encoder.cache_clear()
    monkeypatch.setattr(tm, "ARTIFACT_DIRECTORY", tmp_path)
    try:
        with pytest.raises(CoreError, match="MODEL_TOKEN_ARTIFACT_INVALID"):
            budget.reserve(core.store, core.settings, authorized_job(core),
                "chat", MESSAGES, core.clock())
        assert core.store.rows("SELECT * FROM model_usage") == []
    finally:
        tm._local_encoder.cache_clear()


def test_gzip_container_is_deterministic_and_preserves_official_bytes():
    packed = (tm.ARTIFACT_DIRECTORY / "tokenizer.json.gz").read_bytes()
    raw = gzip.decompress(packed)
    assert hashlib.sha256(raw).hexdigest() == tm.ARTIFACTS["tokenizer.json"]
    assert hashlib.sha256(packed).hexdigest() == tm.TOKENIZER_GZIP_SHA256
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, compresslevel=9, mtime=0) as stream:
        stream.write(raw)
    assert buffer.getvalue() == packed
    assert not (tm.ARTIFACT_DIRECTORY / "tokenizer.json").exists()


def test_corrupt_gzip_rejected_before_decompression(core, tmp_path, monkeypatch):
    configure(core, tmp_path)
    artifacts = tmp_path / "artifacts"
    shutil.copytree(tm.ARTIFACT_DIRECTORY, artifacts)
    (artifacts / "tokenizer.json.gz").write_bytes(b"not a gzip file")
    tm._local_encoder.cache_clear()
    monkeypatch.setattr(tm, "ARTIFACT_DIRECTORY", artifacts)
    try:
        with pytest.raises(CoreError, match="MODEL_TOKEN_ARTIFACT_INVALID"):
            budget.reserve(core.store, core.settings, authorized_job(core),
                "chat", MESSAGES, core.clock())
        assert core.store.rows("SELECT * FROM model_usage") == []
    finally:
        tm._local_encoder.cache_clear()


def test_exact_required_rejects_before_reserve(core, tmp_path):
    configure(core, tmp_path, "exact_required")
    with pytest.raises(CoreError, match="MODEL_TOKEN_COUNT_UNSUPPORTED"):
        budget.reserve(core.store, core.settings, None, "memory_review", MESSAGES, core.clock())
    assert core.store.rows("SELECT * FROM model_usage") == []


@pytest.mark.parametrize("mutation", ["receipt", "payload", "route", "configuration"])
def test_final_transport_hook_cannot_change_count_evidence(core, tmp_path, mutation):
    configure(core, tmp_path)
    request = budget.reserve(core.store, core.settings, authorized_job(core),
        "chat", MESSAGES, core.clock())
    calls = []

    async def hook(req):
        if mutation == "receipt":
            core.store.execute("UPDATE model_admissions SET measurement_json='{}'")
        elif mutation == "payload":
            req._content = json.dumps({**json.loads(req.content), "messages": []}).encode()
        elif mutation == "route":
            core.settings.model_url = "https://different.invalid"
        else:
            core.settings.token_measurement = None

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: calls.append(req) or httpx.Response(500)),
            event_hooks={"request": [hook]},
        ) as client:
            with pytest.raises(CoreError):
                await budget.complete(ModelClient(core.settings, client), core.store,
                                      core.settings, request, MESSAGES, "chat", core.clock)

    asyncio.run(run())
    assert calls == []


def test_journal_detects_measurement_loss_after_settlement(core, tmp_path):
    from companion.model import ModelResponse

    configure(core, tmp_path)
    request = budget.reserve(core.store, core.settings, authorized_job(core),
        "chat", MESSAGES, core.clock())
    budget.settle(core.store, core.settings, request, ModelResponse("ok", 9, 3))
    with core.store.transaction():
        paid_budget.journal(core.store, core.settings.paid_model)
    core.store.execute("UPDATE model_admissions SET measurement_json='{}'")
    with core.store.transaction(), pytest.raises(CoreError,
                                                match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        paid_budget.journal(core.store, core.settings.paid_model)


def test_images_never_claim_text_exactness(core, tmp_path):
    from tests.support.paid_image_budget import image_approval, image_messages

    configure(core, tmp_path)
    image_approval(core, tmp_path, provider="deepseek", model="deepseek-flash",
                   endpoint="https://api.deepseek.com")
    messages = image_messages()
    measured = tm.measure_request(core.settings, messages, "content_vision")
    assert measured.count_kind == "estimate"
    assert measured.input_tokens > 1000
    core.settings.token_measurement = core.settings.token_measurement.model_copy(
        update={"policy": "exact_required"})
    assert tm.measure_request(core.settings, messages, "content_vision").count_kind == "unsupported"


def test_legacy_empty_measurement_keeps_original_journal_contract(core, tmp_path):
    approval = approved_live(core, tmp_path)
    request = budget.reserve(core.store, core.settings, authorized_job(core),
        "chat", MESSAGES, core.clock())
    snapshot = core.store.one("SELECT snapshot_json FROM model_admissions")["snapshot_json"]
    # Reconstruct the pre-0035 journal format from the same reservation fields.
    path = Path(approval.journal_path)
    records = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    records[-1].pop("measurement")
    records[-1].pop("sha256")
    records[-1]["sha256"] = paid_budget.sha(records[-1])
    path.write_text("".join(encode(row) + "\n" for row in records), "utf-8")
    core.store.execute("UPDATE model_admissions SET measurement_json='{}'")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}],
                       "usage": {"prompt_tokens": 20, "completion_tokens": 2}},
        ))) as client:
            await budget.complete(ModelClient(core.settings, client), core.store, core.settings,
                                  request, MESSAGES, "chat", core.clock)

    asyncio.run(run())
    row = core.store.one("SELECT * FROM model_admissions")
    assert row["snapshot_json"] == snapshot
    assert row["measurement_json"] == "{}"
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    with core.store.transaction():
        paid_budget.journal(core.store, approval)


@pytest.mark.parametrize("failure", ["missing", "incompatible"])
def test_unexpected_encoder_load_failure_is_a_clean_refusal(core, tmp_path, monkeypatch, failure):
    import sys
    import types

    configure(core, tmp_path)
    tm._local_encoder.cache_clear()
    if failure == "missing":
        monkeypatch.setitem(sys.modules, "tokenizers", None)  # import raises ImportError
    else:
        class Tokenizer:
            @staticmethod
            def from_str(_):
                raise RuntimeError("synthetic incompatible tokenizer format")

        monkeypatch.setitem(sys.modules, "tokenizers", types.SimpleNamespace(Tokenizer=Tokenizer))
    try:
        with pytest.raises(CoreError, match="MODEL_TOKEN_ARTIFACT_INVALID"):
            budget.reserve(core.store, core.settings, authorized_job(core),
                "chat", MESSAGES, core.clock())
        assert core.store.rows("SELECT * FROM model_usage") == []
    finally:
        tm._local_encoder.cache_clear()


@pytest.mark.parametrize("policy", ["prefer_exact", "exact_required"])
@pytest.mark.parametrize("failure", [NotImplementedError, IndexError, AttributeError])
def test_any_renderer_failure_falls_back_or_refuses(core, tmp_path, monkeypatch, policy, failure):
    configure(core, tmp_path, policy)
    _, tokenizer = tm._local_encoder()

    class Broken:
        def encode_messages(self, *_, **__):
            raise failure("synthetic renderer failure")

    monkeypatch.setattr(tm, "_local_encoder", lambda: (Broken(), tokenizer))
    if policy == "exact_required":
        with pytest.raises(CoreError, match="MODEL_TOKEN_COUNT_UNSUPPORTED"):
            tm.measure_request(core.settings, MESSAGES)
        return
    counted = tm.measure_request(core.settings, MESSAGES)
    assert counted.count_kind == "estimate" and counted.reason == "renderer_rejected_input"
    assert counted.input_tokens > 0

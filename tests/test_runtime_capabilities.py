"""Capability claims follow native state, without mutating or disclosing it."""

import json
from datetime import datetime, timedelta

import pytest

from companion.capabilities import prompt, public_source_readiness, snapshot
from companion.config import ObservationAdapterConfig
from companion.source_grants import SourceGrantRegistration
from tests.support.exploration import configure
from tests.support.paid_model import approved_live
from tests.support.runtime_capabilities import rows
from tests.support.source_grants import GRANT_ID, authorization, prepare


@pytest.fixture
def budget_projection(monkeypatch):
    """Isolate aggregation over deliberately unjournaled synthetic usage rows.

    Real authority admission and read-only custody are tested in
    test_native_capabilities; these tests exercise quota arithmetic only.
    """
    from companion import capabilities

    monkeypatch.setattr(capabilities, "_owner_blocks", lambda *args, **kwargs: [])


def observed(core, **facts):
    return {"observed_at": core.clock(),
            "expires_at": (core.clock.now + timedelta(minutes=2)).isoformat(), **facts}


def grant_native(core, purposes, *, capabilities=()):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from companion import owner_authority, paid_budget
    from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
    from tests.support.owner_authority import activate

    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    hashes = owner_authority.capability_hashes(core.settings)
    policy = OwnerGrantPolicy(owner_qq_id=core.settings.owner_qq_id,
        purposes=purposes, memory_scope="owner_private" if "memory" in purposes else "none",
        authorization_sha256="d" * 64,
        capture={"public_key": Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex(),
                 "identity_sha256": paid_budget.sha(
                     [core.settings.owner_qq_id, core.settings.bot_qq_id]),
                 "protocol_epoch": "native-capability-test"},
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capability_policies={name: hashes[name] for name in capabilities})
    activate(core, policy)


def private_images(core, tmp_path):
    from tests.support.onebot_capture import pinned_plan
    from tests.support.video_understanding import scope_options

    pinned_plan(core, tmp_path, 3001, live=True, approved=True,
                rich_input_scope="references_v1",
                scope_options=scope_options(core, model="synthetic-video"))
    core.settings.meme_discovery_enabled = True
    grant_native(core, ["image", "meme"], capabilities=("image", "meme"))


def test_default_snapshot_is_closed_and_does_not_invent_chat_tools(core):
    current = rows(core)
    assert current["memory.recall"]["state"] == "UNAVAILABLE"
    for feature in ("reading.web", "qq.images", "qq.animation", "memes.search", "media.voice",
                    "media.camera", "media.android", "executive", "model.understand_video"):
        assert current[feature]["state"] == "UNAVAILABLE"
    assert current["unicode.emoji"]["state"] == "READY"
    assert not [row for row in current.values()
                if row["state"] == "READY" and "model_callable" in row["invocation"]]
    text = prompt(snapshot(core.store, core.settings, core.clock))
    assert "系统流程和主人指令不等于模型工具" in text
    assert "字幕不代表看过完整视频" in text
    assert "本轮可调用工具：无" in text
    assert len(text.encode("utf-8")) <= 6000


@pytest.mark.parametrize("change,reason", [
    ("missing", "SOURCE_GRANT_REQUIRED"),
    ("expired", "SOURCE_GRANT_EXPIRED"),
    ("revoked", "SOURCE_GRANT_REVOKED"),
    ("forgotten", "SOURCE_GRANT_FORGOTTEN"),
])
def test_reader_uses_native_latest_grant_rules(core, change, reason):
    grant = prepare(core)
    if change == "missing":
        core.settings.observation_adapters["bili"].source_grant_required = True
        adapter = "bili"
    else:
        adapter = "heybox"
        if change == "expired":
            core.clock.advance(3601)
        elif change == "revoked":
            core.observations.grants.revoke(grant.grant_id)
        else:
            core.store.execute("INSERT INTO runtime_meta VALUES (?, '1')",
                               ("erased_source_grant:" + grant.grant_id,))
    current = public_source_readiness(core.store, core.settings, core.clock, adapter)
    assert current["state"] == "UNAVAILABLE"
    assert reason in current["reason_codes"]
    assert GRANT_ID not in json.dumps(current)


def test_new_valid_grant_is_found_after_expired_revision(core):
    prepare(core)
    core.clock.advance(3601)
    renewed = authorization(core, epoch=2)
    core.observations.grants.register(SourceGrantRegistration(
        adapter_id="heybox", **renewed.snapshot(),
    ))
    current = public_source_readiness(core.store, core.settings, core.clock, "heybox")
    assert current["state"] == "READY"
    assert current["runtime_observation"] == "UNOBSERVED"
    assert current["reason_codes"] == []


def test_reader_observations_never_turn_registration_into_online_proof(core):
    prepare(core)
    fresh = observed(core, accepting_tasks=True, subtitles=True, discussion=True)
    current = rows(core, integrations={"readers": {"heybox": fresh}})
    assert current["reading.heybox"]["state"] == "READY"
    assert current["reading.heybox"]["runtime_observation"] == "OBSERVED"
    paused = public_source_readiness(core.store, core.settings, core.clock, "heybox",
                                    integration=observed(core, accepting_tasks=False))
    assert paused["state"] == "PAUSED"
    core.clock.advance(121)
    stale = public_source_readiness(core.store, core.settings, core.clock, "heybox",
                                   integration=fresh)
    assert stale["state"] == "PAUSED" and stale["runtime_observation"] == "STALE"
    assert "READER_OBSERVATION_STALE" in stale["reason_codes"]


def test_reader_readiness_reserves_full_native_request_allowance(core):
    configure(core)
    core.settings.observation_adapters["bili"].reader_max_requests = 8
    core.settings.exploration_daily_requests = 7
    current = public_source_readiness(core.store, core.settings, core.clock, "bili")
    assert current["state"] == "PAUSED"
    assert "READ_BUDGET_UNAVAILABLE" in current["reason_codes"]
    core.settings.exploration_daily_requests = 8
    current = public_source_readiness(core.store, core.settings, core.clock, "bili")
    assert current["state"] == "READY"


def test_life_pause_does_not_disable_explicit_source_reads(core, tmp_path):
    approved_live(core, tmp_path)
    prepare(core)
    core.settings.diary_enabled = True
    core.settings.memory_enabled = True
    core.settings.consolidation_enabled = True
    grant_native(core, ["memory", "diary", "consolidation", "content", "exploration"])
    core.store.execute("INSERT OR REPLACE INTO runtime_meta VALUES ('life_paused','1')")
    core.store.execute("INSERT OR REPLACE INTO runtime_meta VALUES ('diary_paused','1')")
    current = rows(core)
    assert current["reading.heybox"]["state"] == "READY"
    assert current["memory.manage"]["state"] == "READY"
    assert current["memory.recall"]["state"] == "READY"
    assert current["consolidation"]["state"] == "PAUSED"
    assert "LIFE_PAUSED" in current["consolidation"]["reason_codes"]
    assert current["diary"]["state"] == "PAUSED"
    assert "DIARY_PAUSED" in current["diary"]["reason_codes"]
    assert current["exploration"]["state"] == "PAUSED"


@pytest.mark.parametrize("key", ["model_budget_blocked", "model_budget_restore_required"])
def test_existing_budget_block_is_reported_without_reconciliation(core, tmp_path, key):
    approved_live(core, tmp_path)
    core.settings.memory_enabled = True
    grant_native(core, ["memory"])
    before_journal = (tmp_path / "budget.jsonl").read_bytes()
    core.store.execute("INSERT INTO runtime_meta VALUES (?, '1')", (key,))
    before = list(core.store.db.iterdump())
    current = rows(core)
    assert current["memory.recall"]["state"] == "PAUSED"
    assert "BUDGET_RECONCILIATION_REQUIRED" in current["memory.recall"]["reason_codes"]
    assert list(core.store.db.iterdump()) == before
    assert (tmp_path / "budget.jsonl").read_bytes() == before_journal


def test_search_provider_none_keeps_local_catalog_and_community_admission(core, tmp_path):
    private_images(core, tmp_path)
    core.settings.meme_discovery_enabled = True
    evidence = observed(core, catalog_ready=True, community_import=True,
                        search_provider="none", search_ready=False)
    current = rows(core, integrations={"bridge_memes": evidence})
    assert current["memes.catalog"]["state"] == "READY"
    assert current["memes.community_import"]["state"] == "READY"
    assert current["memes.search"]["state"] == "UNAVAILABLE"
    absent = rows(core)
    assert absent["memes.catalog"]["state"] == "READY"
    assert absent["memes.community_import"]["state"] == "READY"
    assert absent["memes.catalog"]["runtime_observation"] == "UNOBSERVED"
    assert "实际素材" in absent["memes.catalog"]["summary"]
    assert len(prompt(snapshot(core.store, core.settings, core.clock)).encode("utf-8")) <= 6000
    paused = rows(core, integrations={"bridge_memes": observed(core, catalog_ready=False,
                                                              community_import=False)})
    assert paused["memes.catalog"]["state"] == "PAUSED"
    assert paused["memes.community_import"]["state"] == "PAUSED"


def test_face_image_admission_uses_native_image_scope_and_observation_is_separate(core, tmp_path):
    core.settings.rich_input_scope = "references_v1"
    current = rows(core)
    assert current["qq.face_names"]["state"] == "READY"
    assert current["qq.face_images"]["state"] == "UNAVAILABLE"
    private_images(core, tmp_path)
    current = rows(core)
    assert current["qq.face_images"]["state"] == "READY"
    assert current["qq.face_images"]["runtime_observation"] == "UNOBSERVED"
    current = rows(core, integrations={"qq_faces": observed(core, static_images=True)})
    assert current["qq.face_images"]["state"] == "READY"
    assert current["qq.face_images"]["runtime_observation"] == "OBSERVED"
    current = rows(core, integrations={"qq_faces": observed(core, static_images=False)})
    assert current["qq.face_images"]["state"] == "PAUSED"


def test_projection_does_not_echo_configuration_grant_or_integration_secrets(core, tmp_path):
    approved_live(core, tmp_path)
    prepare(core)
    core.settings.meme_discovery_enabled = True
    data = {"secret": "do-not-echo", "model_key": "private-model-key",
            "endpoint": "https://secret.invalid/private", "owner_qq_id": "secret-owner"}
    evidence = observed(core, catalog_ready=True, community_import=True, **data)
    before = list(core.store.db.iterdump())
    result = snapshot(core.store, core.settings, core.clock,
                      integrations={"bridge_memes": evidence, **data})
    rendered = json.dumps(result, ensure_ascii=False) + prompt(result)
    for secret in (*data.values(), "synthetic-key", GRANT_ID, "a" * 64,
                   core.settings.bridge_token.get_secret_value(), str(tmp_path)):
        assert secret not in rendered
    assert list(core.store.db.iterdump()) == before


def test_tools_argument_never_bypasses_native_video_approval(core):
    for supplied in (("understand_video",),
                     ({"type": "function", "function": {"name": "understand_video"}},)):
        current = rows(core, model_tools=supplied)
        assert current["model.understand_video"]["state"] == "UNAVAILABLE"
        assert "PAID_VIDEO_APPROVAL_REQUIRED" in current["model.understand_video"]["reason_codes"]
    assert rows(core, model_tools=("arbitrary_tool",)).keys() == rows(core).keys()


def test_native_video_requires_actual_tool_exposure(core, tmp_path):
    from companion import owner_authority
    from tests.support.owner_authority import activate

    private_images(core, tmp_path)
    denied = rows(core, model_tools=("understand_video",))
    for feature in ("qq.animation", "model.understand_video"):
        assert denied[feature]["state"] == "UNAVAILABLE"
        assert "OWNER_AUTHORITY_PURPOSE_REQUIRED" in denied[feature]["reason_codes"]
    view = owner_authority.current(core.store, core.settings, core.clock)
    policy = view.policy.model_copy(update={"purposes": [*view.policy.purposes, "content"]})
    activate(core, policy, revision=view.authority.revision, operation="video-content-permission")
    chat = rows(core)
    assert chat["qq.animation"]["state"] == "READY"
    assert chat["bilibili.video"]["state"] == "UNAVAILABLE"
    assert chat["model.understand_video"]["state"] == "UNAVAILABLE"
    vision = rows(core, model_tools=("understand_video",))
    assert vision["model.understand_video"]["state"] == "READY"
    assert vision["model.understand_video"]["invocation"] == ["model_callable"]


def test_native_bili_tool_requires_current_permission_and_actual_exposure(core, tmp_path):
    native_capabilities(core, tmp_path)
    assert "tool.analyze_shared_video" not in rows(core)
    current = rows(core, model_tools=("analyze_shared_video",))
    assert current["tool.analyze_shared_video"]["state"] == "READY"
    core.settings.bili_video = core.settings.bili_video.model_copy(update={"enabled": False})
    current = rows(core, model_tools=("analyze_shared_video",))
    assert current["tool.analyze_shared_video"]["state"] == "UNAVAILABLE"


def test_native_bili_tool_requires_content_even_when_given_as_exposed(core, tmp_path):
    from companion import owner_authority
    from tests.support.owner_authority import activate

    native_capabilities(core, tmp_path)
    view = owner_authority.current(core.store, core.settings, core.clock)
    policy = view.policy.model_copy(update={
        "purposes": [name for name in view.policy.purposes if name != "content"]})
    activate(core, policy, revision=view.authority.revision, operation="bili-content-withdrawal")
    fact = rows(core, model_tools=("analyze_shared_video",))["tool.analyze_shared_video"]
    assert fact["state"] == "UNAVAILABLE"
    assert "OWNER_AUTHORITY_PURPOSE_REQUIRED" in fact["reason_codes"]


def test_retired_fact_window_does_not_claim_native_memory_admission(core, tmp_path):
    from tests.support.live_memory_recall import scope_facts
    from tests.support.onebot_capture import pinned_plan

    _, _, pins = scope_facts(core)
    pinned_plan(core, tmp_path, 3001, live=True, approved=True, scope_options={
        "memory_scope": "approved_facts", "memory_facts": pins,
        "memory_approval_sha256": "f" * 64, "core_options": {"memory_enabled": True},
        "paid_options": {"per_request_calls": 2},
    })
    current = rows(core)
    assert current["memory.recall"]["state"] == "UNAVAILABLE"
    assert current["memory.manage"]["state"] == "UNAVAILABLE"
    assert "OWNER_AUTHORITY_DISABLED" in current["memory.manage"]["reason_codes"]


def test_discovery_does_not_consume_deep_read_capacity_but_keeps_native_resource_limits(core):
    configure(core)
    cfg = core.settings
    cfg.exploration_daily_deep_reads = 0
    assert public_source_readiness(core.store, cfg, core.clock, "bili")["state"] == "PAUSED"
    assert public_source_readiness(core.store, cfg, core.clock, "bili",
                                   operation="discover")["state"] == "READY"
    assert rows(core)["exploration"]["state"] == "READY"
    cfg.observation_adapters["bili"].reader_max_requests = 8
    cfg.exploration_daily_requests = 7
    current = public_source_readiness(core.store, cfg, core.clock, "bili", operation="discover")
    assert current["state"] == "PAUSED"
    assert "READ_BUDGET_UNAVAILABLE" in current["reason_codes"]
    cfg.exploration_daily_requests = 8
    cfg.exploration_daily_bytes = cfg.exploration_max_response_bytes - 1
    current = public_source_readiness(core.store, cfg, core.clock, "bili", operation="discover")
    assert current["state"] == "PAUSED"
    assert "READ_BUDGET_UNAVAILABLE" in current["reason_codes"]


def test_discovery_keeps_source_grant_scope_and_web_is_read_only(core):
    prepare(core)
    cfg = core.settings
    cfg.exploration_daily_deep_reads = 0
    core.observations.grants.revoke(GRANT_ID)
    current = public_source_readiness(core.store, cfg, core.clock, "heybox", operation="discover")
    assert current["state"] == "UNAVAILABLE"
    assert "SOURCE_GRANT_REVOKED" in current["reason_codes"]
    cfg.content_enabled = cfg.web_reading_enabled = True
    cfg.web_reading_hosts = ("example.com",)
    cfg.observation_adapters["web"] = ObservationAdapterConfig(
        source="web", token="synthetic-web-reader-token-12345",
    )
    cfg.exploration_daily_deep_reads = 5
    assert public_source_readiness(core.store, cfg, core.clock, "web")["state"] == "READY"
    current = public_source_readiness(core.store, cfg, core.clock, "web", operation="discover")
    assert current["state"] == "UNAVAILABLE"
    assert "READER_OPERATION_UNAVAILABLE" in current["reason_codes"]


def reflection_settings(core, tmp_path, *, unlimited=False):
    approved_live(core, tmp_path, **({
        "unlimited_period_spend": True, "daily_micro_usd": None,
        "monthly_micro_usd": None, "window_micro_usd": None,
    } if unlimited else {}))
    core.settings.reflection_enabled = True
    core.settings.reflection_daily_micro_usd = 100
    core.settings.reflection_monthly_micro_usd = 1000


def usage(core, *, state="SETTLED", amount=1, at=None, scenario="reflection"):
    number = core.store.one("SELECT COUNT(*) n FROM model_usage")["n"]
    core.store.insert(
        "model_usage", usage_id=f"usage_capability_{number}",
        request_id=f"request_capability_{number}", scenario=scenario,
        provider=core.settings.provider, model=core.settings.model_name,
        state=state, billed_micro_usd=amount, occurred_at=at or core.clock(),
    )
    return f"request_capability_{number}"


@pytest.mark.parametrize("state", ["SETTLED", "RESERVED", "UNKNOWN"])
@pytest.mark.parametrize("unlimited", [False, True])
@pytest.mark.usefixtures("budget_projection")
def test_reflection_capability_counts_every_attempt_even_with_unlimited_spend(
    core, tmp_path, state, unlimited,
):
    reflection_settings(core, tmp_path, unlimited=unlimited)
    assert rows(core)["reflection"]["state"] == "READY"
    usage(core, state=state)
    before = list(core.store.db.iterdump())
    current = rows(core)["reflection"]
    assert current["state"] == "PAUSED"
    assert "SCENARIO_CALL_LIMIT" in current["reason_codes"]
    assert current["limits"]["max_daily_calls"] == 1
    assert list(core.store.db.iterdump()) == before
    assert not (tmp_path / "budget.jsonl").exists()


@pytest.mark.parametrize("state", ["SETTLED", "RESERVED", "UNKNOWN"])
@pytest.mark.parametrize("period,reason", [
    ("daily", "DAILY_BUDGET_EXHAUSTED"),
    ("monthly", "SCENARIO_MONTHLY_BUDGET_EXHAUSTED"),
])
@pytest.mark.usefixtures("budget_projection")
def test_reflection_capability_includes_all_accounted_amounts(
    core, tmp_path, state, period, reason,
):
    reflection_settings(core, tmp_path)
    core.settings.reflection_max_daily_calls = 5
    at = None if period == "daily" else "2026-10-18T04:00:00.000000+00:00"
    amount = getattr(core.settings, "reflection_" + period + "_micro_usd")
    usage(core, state=state, amount=amount, at=at)
    current = rows(core)["reflection"]
    assert current["state"] == "PAUSED"
    assert reason in current["reason_codes"]
    assert "SCENARIO_CALL_LIMIT" not in current["reason_codes"]
    if period == "monthly":
        assert "DAILY_BUDGET_EXHAUSTED" not in current["reason_codes"]


@pytest.mark.usefixtures("budget_projection")
def test_reflection_capability_unlimited_spend_does_not_remove_call_limit(core, tmp_path):
    reflection_settings(core, tmp_path, unlimited=True)
    core.settings.reflection_max_daily_calls = 2
    usage(core, amount=1000)
    current = rows(core)["reflection"]
    assert current["state"] == "READY"
    assert current["reason_codes"] == []
    usage(core, amount=1000)
    current = rows(core)["reflection"]
    assert current["state"] == "PAUSED"
    assert current["reason_codes"] == ["SCENARIO_CALL_LIMIT"]


@pytest.mark.parametrize("period", ["daily", "monthly"])
@pytest.mark.usefixtures("budget_projection")
def test_reflection_capability_zero_amount_stays_disabled_with_unlimited_spend(
    core, tmp_path, period,
):
    reflection_settings(core, tmp_path, unlimited=True)
    setattr(core.settings, "reflection_" + period + "_micro_usd", 0)
    current = rows(core)["reflection"]
    assert current["state"] == "PAUSED"
    assert current["reason_codes"] == ["SCENARIO_BUDGET_DISABLED"]


@pytest.mark.parametrize("now,at,reasons", [
    ("2026-11-01T00:00:00+08:00", "2026-10-31T15:59:59.999999+00:00", []),
    ("2026-11-01T00:00:00+08:00", "2026-10-31T16:00:00.000000+00:00",
     ["SCENARIO_CALL_LIMIT", "DAILY_BUDGET_EXHAUSTED", "SCENARIO_MONTHLY_BUDGET_EXHAUSTED"]),
    ("2026-10-19T00:00:00+08:00", "2026-10-18T15:59:59.999999+00:00",
     ["SCENARIO_MONTHLY_BUDGET_EXHAUSTED"]),
])
@pytest.mark.usefixtures("budget_projection")
def test_reflection_capability_uses_local_day_and_month_boundaries(
    core, tmp_path, now, at, reasons,
):
    reflection_settings(core, tmp_path)
    core.clock.now = datetime.fromisoformat(now)
    usage(core, amount=1000, at=at)
    current = rows(core)["reflection"]
    assert current["reason_codes"] == reasons
    assert current["state"] == ("PAUSED" if reasons else "READY")


@pytest.mark.usefixtures("budget_projection")
def test_reflection_capability_does_not_charge_other_scenarios(core, tmp_path):
    reflection_settings(core, tmp_path)
    for scenario in ("chat", "diary", "memory_review"):
        usage(core, amount=1000, scenario=scenario)
    current = rows(core)["reflection"]
    assert current["state"] == "READY"
    assert current["reason_codes"] == []


def paid_capacity_settings(core, tmp_path, **changes):
    approval = approved_live(core, tmp_path, **{
        "daily_micro_usd": 1000000, "monthly_micro_usd": 10000000,
        "window_micro_usd": 1000000, "daily_calls": 20,
        "window_calls": 10, "concurrency": 10, **changes,
    })
    core.settings.memory_enabled = True
    return approval


def admission(core, request_id, *, window_id=None):
    approval = core.settings.paid_model
    window_id = window_id or approval.window_id
    core.store.execute("INSERT OR IGNORE INTO model_budget_windows VALUES (?,?)",
                       (window_id, "a" * 64))
    core.store.insert(
        "model_admissions", request_id=request_id, logical_request_id=request_id,
        attempt_id="attempt_" + request_id, attempt_number=1, window_id=window_id,
        contract_sha256="a" * 64, payload_sha256="b" * 64, snapshot_json="{}",
        reserved_micro_usd=1, input_bound=1, output_bound=1, lease_token="fixture-lease",
        lease_until="2000-01-01T00:00:00.000000+00:00",
        journal_path=approval.journal_path, journal_id="budget_" + "a" * 32,
    )


def tool_charge(core, amount, *, at=None):
    number = core.store.one("SELECT COUNT(*) n FROM tool_tasks")["n"]
    core.store.insert(
        "tool_tasks", task_id=f"tool_capability_{number}", idempotency_key=f"tool-key-{number}",
        request_sha256="a" * 64, scope_sha256="b" * 64, executor_id="synthetic",
        capability="dsh.run", scope_json="{}", risk_level=3, state="UNKNOWN",
        captured_privacy_epoch=0, billed_micro_usd=amount, dispatched_at=at or core.clock(),
        created_at=core.clock(), updated_at=core.clock(),
    )


@pytest.mark.parametrize("state", ["SETTLED", "RESERVED", "UNKNOWN"])
@pytest.mark.usefixtures("budget_projection")
def test_paid_capacity_daily_amount_counts_preexisting_other_window_usage(core, tmp_path, state):
    approval = paid_capacity_settings(core, tmp_path, daily_micro_usd=100)
    request = usage(core, state=state, amount=100, scenario="chat")
    admission(core, request, window_id="previous-paid-window")
    assert not core.store.one("SELECT 1 FROM model_admissions WHERE window_id=?",
                              (approval.window_id,))
    before = list(core.store.db.iterdump())
    current = rows(core)["memory.recall"]
    assert current["state"] == "PAUSED"
    assert "MODEL_DAILY_BUDGET_EXHAUSTED" in current["reason_codes"]
    assert "MODEL_WINDOW_BUDGET_EXHAUSTED" not in current["reason_codes"]
    assert list(core.store.db.iterdump()) == before
    assert not (tmp_path / "budget.jsonl").exists()


@pytest.mark.parametrize("state", ["RESERVED", "UNKNOWN"])
@pytest.mark.usefixtures("budget_projection")
def test_paid_capacity_requires_reconciliation_for_legacy_unadmitted_usage(core, tmp_path, state):
    paid_capacity_settings(core, tmp_path, daily_micro_usd=100)
    usage(core, state=state, amount=100, scenario="content_video")
    current = rows(core)["memory.recall"]
    assert current["state"] == "PAUSED"
    assert "MODEL_LEGACY_USAGE_RECONCILIATION_REQUIRED" in current["reason_codes"]
    assert "MODEL_DAILY_BUDGET_EXHAUSTED" in current["reason_codes"]
    assert "MODEL_PROVIDER_CONCURRENCY_EXHAUSTED" not in current["reason_codes"]


@pytest.mark.usefixtures("budget_projection")
def test_paid_capacity_unlimited_daily_calls_still_count_all_model_scenarios(core, tmp_path):
    paid_capacity_settings(core, tmp_path, unlimited_period_spend=True, daily_micro_usd=None,
                           monthly_micro_usd=None, window_micro_usd=None, daily_calls=2)
    core.settings.cloud_budget.daily_micro_cny = 2_000_000_000
    usage(core, amount=100000000, scenario="diary")
    usage(core, amount=100000000, scenario="chat")
    current = rows(core)["memory.recall"]
    assert current["state"] == "PAUSED"
    assert current["reason_codes"] == ["MODEL_DAILY_BUDGET_EXHAUSTED"]


@pytest.mark.parametrize("ceiling,reason", [
    ("paid", "MODEL_MONTHLY_BUDGET_EXHAUSTED"),
    ("local", "MONTHLY_BUDGET_EXHAUSTED"),
])
@pytest.mark.usefixtures("budget_projection")
def test_paid_capacity_monthly_totals_include_tool_responsibility(core, tmp_path, ceiling, reason):
    paid_capacity_settings(core, tmp_path, monthly_micro_usd=500 if ceiling == "paid" else 1000)
    core.settings.monthly_limit_micro_usd = 500 if ceiling == "local" else 1000
    usage(core, amount=200, scenario="chat")
    tool_charge(core, 300)
    tool_charge(core, 1000000, at="2026-09-30T15:59:59.999999+00:00")
    current = rows(core)["memory.recall"]
    assert current["state"] == "PAUSED"
    assert current["reason_codes"] == [reason]


@pytest.mark.parametrize("ceiling", ["amount", "calls"])
@pytest.mark.usefixtures("budget_projection")
def test_paid_capacity_window_totals_use_only_current_window(core, tmp_path, ceiling):
    paid_capacity_settings(core, tmp_path, window_micro_usd=100 if ceiling == "amount" else 1000,
                           window_calls=2 if ceiling == "calls" else 10)
    previous = usage(core, amount=500, scenario="chat")
    admission(core, previous, window_id="previous-paid-window")
    assert rows(core)["memory.recall"]["state"] == "READY"
    first = usage(core, amount=50, scenario="chat")
    admission(core, first)
    assert rows(core)["memory.recall"]["state"] == "READY"
    second = usage(core, amount=50, scenario="chat")
    admission(core, second)
    current = rows(core)["memory.recall"]
    assert current["state"] == "PAUSED"
    assert current["reason_codes"] == ["MODEL_WINDOW_BUDGET_EXHAUSTED"]


@pytest.mark.parametrize("state", ["RESERVED", "UNKNOWN"])
@pytest.mark.parametrize("unlimited", [False, True])
@pytest.mark.usefixtures("budget_projection")
def test_paid_capacity_concurrency_does_not_expire_with_lease_or_window(
    core, tmp_path, state, unlimited,
):
    paid_capacity_settings(core, tmp_path, concurrency=2, **({
        "unlimited_period_spend": True, "daily_micro_usd": None,
        "monthly_micro_usd": None, "window_micro_usd": None,
    } if unlimited else {}))
    for window in ("previous-paid-window", core.settings.paid_model.window_id):
        request = usage(core, state=state, scenario="chat")
        admission(core, request, window_id=window)
    current = rows(core)["memory.recall"]
    assert current["state"] == "PAUSED"
    assert "MODEL_PROVIDER_CONCURRENCY_EXHAUSTED" in current["reason_codes"]
    assert "MODEL_LEGACY_USAGE_RECONCILIATION_REQUIRED" not in current["reason_codes"]


@pytest.mark.usefixtures("budget_projection")
def test_paid_capacity_unlimited_amounts_leave_window_call_limit_intact(core, tmp_path):
    paid_capacity_settings(core, tmp_path, unlimited_period_spend=True, daily_micro_usd=None,
                           monthly_micro_usd=None, window_micro_usd=None, window_calls=2)
    core.settings.cloud_budget.daily_micro_cny = 10_000_000_000
    core.settings.monthly_limit_micro_usd = 1
    first = usage(core, amount=100000000, scenario="chat")
    admission(core, first)
    tool_charge(core, 100000000)
    assert rows(core)["memory.recall"]["state"] == "READY"
    second = usage(core, amount=100000000, scenario="chat")
    admission(core, second)
    current = rows(core)["memory.recall"]
    assert current["state"] == "PAUSED"
    assert current["reason_codes"] == ["MODEL_WINDOW_BUDGET_EXHAUSTED"]


@pytest.mark.usefixtures("budget_projection")
def test_paid_capacity_daily_limit_resets_at_local_midnight(core, tmp_path):
    paid_capacity_settings(core, tmp_path, daily_micro_usd=100)
    core.clock.now = datetime.fromisoformat("2026-10-19T23:59:59.999999+08:00")
    usage(core, amount=100, scenario="chat", at="2026-10-19T15:59:59.999999+00:00")
    assert "MODEL_DAILY_BUDGET_EXHAUSTED" in rows(core)["memory.recall"]["reason_codes"]
    core.clock.now += timedelta(microseconds=1)
    assert rows(core)["memory.recall"]["state"] == "READY"


@pytest.mark.usefixtures("budget_projection")
def test_paid_capacity_does_not_guess_the_next_request_estimate(core, tmp_path):
    paid_capacity_settings(core, tmp_path, daily_micro_usd=100, monthly_micro_usd=100,
                           window_micro_usd=100)
    core.settings.monthly_limit_micro_usd = 100
    request = usage(core, amount=99, scenario="chat")
    admission(core, request)
    current = rows(core)["memory.recall"]
    assert current["state"] == "READY"
    assert current["reason_codes"] == []


def native_capabilities(core, tmp_path):
    from companion import paid_budget
    from companion.file_evidence import PrivateFilesConfig
    from companion.poke_protocol import PokePolicy
    from companion.social_autonomy import SocialAutonomy
    from companion.social_autonomy_protocol import ProfileBaseline, SocialAutonomyConfig
    from tests.support.onebot_capture import pinned_plan
    from tests.support.social_autonomy import avatar
    from tests.support.voice_jobs import voice_config
    from tests.test_bili_video import paid_options

    options = paid_options(core)
    files = PrivateFilesConfig(enabled=True, parser_image="sha256:" + "d" * 64,
                               parser_socket="/run/private-parser/parser.sock")
    voice = voice_config()
    poke = PokePolicy(enabled=True, allow_native=True)
    social = SocialAutonomyConfig(enabled=True, scope="qq_social_v1",
        authorization_sha256="a" * 64, qzone_enabled=True, nickname_enabled=True,
        signature_enabled=True, avatar_enabled=True, asset_policy_sha256="e" * 64)
    options.update(file_scope="owner_file_v1", voice_scope="owner_voice_v1",
        voice_policy_sha256=voice.authority_sha256(), poke_scope="owner_poke_v1",
        poke_policy_sha256=paid_budget.sha(poke.model_dump()), account_actions_scope="qq_social_v1",
        account_actions_authorization_sha256=social.authorization_sha256)
    options["core_options"].update(private_files=files, voice=voice, poke_policy=poke,
                                   social_autonomy=social)
    options["core_options"]["observation_adapters"][files.adapter_id] = ObservationAdapterConfig(
        source="qq_file", token="synthetic-file-adapter-token-123456")
    pinned_plan(core, tmp_path, 3001, live=True, approved=True,
                rich_input_scope="references_v1", scope_options=options)
    grant_native(core, ["content", "exploration", "image", "file", "voice", "poke",
                        "account_actions", "social", "bili_video"],
        capabilities=("image", "file", "voice", "poke", "account_actions", "bili_video"))
    module = SocialAutonomy(core.store, core.settings, clock=core.clock)
    for field in ("nickname", "signature", "avatar"):
        module.record_profile(ProfileBaseline(bot_qq_id=core.settings.bot_qq_id,
            field=field, value="0" * 64 if field == "avatar" else "synthetic original",
            observed_at=core.clock(), receipt_sha256="b" * 64))
    module.register_asset(avatar(core))
    return module


NATIVE_CAPABILITIES = (
    "bilibili.video", "qq.files", "qq.voice.asr", "qq.voice.tts", "qq.poke", "qq.poke.native",
    "qq.account.qzone_publish", "qq.account.nickname", "qq.account.signature",
    "qq.account.avatar", "qq.profile.observe",
)


def test_native_capabilities_are_local_facts_without_guard_or_external_io(
    core, tmp_path, monkeypatch,
):
    from companion import budget, live_text
    from companion.social_autonomy import SocialAutonomy

    native_capabilities(core, tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("capability projection cannot authorize or execute")

    monkeypatch.setattr(live_text, "guard", forbidden)
    monkeypatch.setattr(budget, "reserve", forbidden)
    monkeypatch.setattr(SocialAutonomy, "_authorized", forbidden)
    before = list(core.store.db.iterdump())
    current = rows(core)
    assert all(current[name]["state"] == "READY" for name in NATIVE_CAPABILITIES)
    assert all(current[name]["runtime_observation"] == "UNOBSERVED"
               for name in ("bilibili.video", "qq.files", "qq.voice.asr", "qq.voice.tts"))
    assert current["media.voice"]["state"] == "UNAVAILABLE"
    assert current["model.understand_video"]["state"] == "UNAVAILABLE"
    assert all("model_callable" not in current[name]["invocation"] for name in NATIVE_CAPABILITIES)
    assert list(core.store.db.iterdump()) == before and core.model.calls == []
    for compact in (False, True):
        text = prompt(snapshot(core.store, core.settings, core.clock), compact=compact)
        assert "视频画面分析未接入" not in text and "画面分析尚未接入" not in text
        assert "字幕不代表看过完整视频" in text and "采样" in text
        assert len(text.encode()) <= (3000 if compact else 6000)
        assert "qq.files" in text and "qq.voice.asr" in text and "bilibili.video" in text
    safe = json.dumps(current, ensure_ascii=False)
    token = core.settings.voice.nodes["local"].token.get_secret_value()
    for secret in ("a" * 64, "d" * 64, str(tmp_path), "/run/private-parser",
                   "synthetic original", token):
        assert secret not in safe


@pytest.mark.parametrize("feature,scope", [
    ("qq.files", "file_scope"), ("bilibili.video", "bili_video_scope"),
    ("qq.voice.asr", "voice_scope"), ("qq.voice.tts", "voice_scope"),
    ("qq.poke", "poke_scope"), ("qq.account.nickname", "account_actions_scope"),
])
def test_feature_toggle_without_original_scope_never_claims_ready(core, tmp_path, feature, scope):
    from companion import owner_authority
    from tests.support.owner_authority import activate

    native_capabilities(core, tmp_path)
    purpose = {"file_scope": "file", "bili_video_scope": "bili_video", "voice_scope": "voice",
               "poke_scope": "poke", "account_actions_scope": "account_actions"}[scope]
    view = owner_authority.current(core.store, core.settings, core.clock)
    policy = view.policy.model_copy(update={
        "purposes": [name for name in view.policy.purposes if name != purpose],
        "capability_policies": {name: value for name, value in
            view.policy.capability_policies.items() if name != purpose}})
    activate(core, policy, revision=view.authority.revision, operation="capability-withdrawal")
    assert rows(core)[feature]["state"] == "UNAVAILABLE"


@pytest.mark.parametrize("change,reason", [
    ("image", "PRIVATE_FILE_CONFIG_INVALID"), ("socket", "PRIVATE_FILE_CONFIG_INVALID"),
    ("bytes", "PRIVATE_FILE_TRAFFIC_EXHAUSTED"),
])
def test_files_require_pinned_parser_transport_adapter_and_original_traffic_budget(
    core, tmp_path, change, reason,
):
    native_capabilities(core, tmp_path)
    cfg = core.settings.private_files
    if change == "image":
        core.settings.private_files = cfg.model_copy(update={"parser_image": "floating:latest"})
    elif change == "socket":
        core.settings.private_files = cfg.model_copy(update={"parser_socket": "../unsafe.sock"})
    else:
        core.settings.private_files = cfg.model_copy(
            update={"max_daily_bytes": cfg.max_file_bytes - 1})
    before = list(core.store.db.iterdump())
    fact = rows(core)["qq.files"]
    assert reason in fact["reason_codes"] and fact["state"] != "READY"
    assert list(core.store.db.iterdump()) == before


@pytest.mark.parametrize("change,reason", [
    ("reader", "BILI_VIDEO_READER_UNAVAILABLE"), ("image", "PAID_IMAGE_APPROVAL_REQUIRED"),
    ("provider", "PAID_VIDEO_APPROVAL_REQUIRED"), ("policy", "OWNER_AUTHORITY_CAPABILITY_CHANGED"),
    ("bytes", "READ_BUDGET_UNAVAILABLE"), ("requests", "READ_BUDGET_UNAVAILABLE"),
    ("restore", "BILI_VIDEO_RECONCILIATION_REQUIRED"),
])
def test_bili_sampling_follows_media_scope_provider_reader_and_full_reservation(
    core, tmp_path, change, reason,
):
    from pydantic import SecretStr

    native_capabilities(core, tmp_path)
    cfg = core.settings
    if change == "reader":
        cfg.observation_adapters["bili"].media_analysis = False
    elif change == "image":
        cfg.paid_model = cfg.paid_model.model_copy(update={"image_input": None})
    elif change == "provider":
        cfg.video_model_key = SecretStr("")
    elif change == "policy":
        cfg.bili_video = cfg.bili_video.model_copy(update={"max_concurrent": 2})
    elif change == "bytes":
        cfg.exploration_daily_bytes = cfg.bili_video.media.max_input_bytes
    elif change == "requests":
        cfg.exploration_daily_requests = 7
    else:
        core.store.execute("INSERT INTO runtime_meta VALUES ('bili_video_restore_required','1')")
    fact = rows(core)["bilibili.video"]
    assert fact["state"] != "READY" and reason in fact["reason_codes"]


@pytest.mark.parametrize("key", ["model_budget_restore_required", "model_budget_blocked"])
def test_native_readiness_preserves_restore_and_independent_budget_blocks(core, tmp_path, key):
    native_capabilities(core, tmp_path)
    core.store.execute("INSERT INTO runtime_meta VALUES (?, '1')", (key,))
    before = list(core.store.db.iterdump())
    current = rows(core)
    assert all(current[name]["state"] == "PAUSED" for name in NATIVE_CAPABILITIES)
    assert list(core.store.db.iterdump()) == before


def test_voice_observation_is_separate_from_lease_and_resource_admission(core, tmp_path):
    native_capabilities(core, tmp_path)
    core.store.insert("voice_node_state", node_id="local", resource_state="READY",
                      last_seen_at=core.clock())
    fact = rows(core)["qq.voice.asr"]
    assert fact["state"] == "READY" and fact["runtime_observation"] == "OBSERVED"
    core.clock.advance(core.settings.voice.lease_ttl_seconds + 1)
    fact = rows(core)["qq.voice.asr"]
    assert fact["state"] == "READY" and fact["runtime_observation"] == "STALE"
    core.store.execute("UPDATE voice_node_state SET resource_state='QUARANTINED'")
    fact = rows(core)["qq.voice.asr"]
    assert fact["state"] == "PAUSED"
    assert "VOICE_RESOURCE_RECONCILIATION_REQUIRED" in fact["reason_codes"]


def test_account_state_uses_real_profile_evidence_native_cooldown_and_cached_observation_wording(
    core, tmp_path,
):
    native_capabilities(core, tmp_path)
    core.store.execute("DELETE FROM social_autonomy_profiles WHERE field='nickname'")
    fact = rows(core)["qq.account.nickname"]
    assert fact["state"] == "PAUSED"
    assert "SOCIAL_PROFILE_BASELINE_REQUIRED" in fact["reason_codes"]
    assert "缓存" in rows(core)["qq.profile.observe"]["summary"]
    assert "不证明网络刷新" in rows(core)["qq.profile.observe"]["summary"]
    core.store.execute("INSERT INTO runtime_meta VALUES ('social_autonomy_restore_required','1')")
    assert rows(core)["qq.account.qzone_publish"]["state"] == "PAUSED"


def test_unknown_model_liability_pauses_native_capabilities_without_settling_it(core, tmp_path):
    native_capabilities(core, tmp_path)
    usage(core, state="UNKNOWN", scenario="chat")
    before = list(core.store.db.iterdump())
    current = rows(core)
    assert all(current[name]["state"] == "PAUSED" for name in NATIVE_CAPABILITIES)
    assert "MODEL_USAGE_UNKNOWN" in current["qq.voice.asr"]["reason_codes"]
    assert list(core.store.db.iterdump()) == before


@pytest.mark.usefixtures("budget_projection")
def test_cumulative_pause_is_read_from_original_journal_without_repair(core, tmp_path):
    from tests.support.delivery_reconciliation import native

    native(core, tmp_path)
    before = list(core.store.db.iterdump())
    result = rows(core)["memory.recall"]
    assert result["state"] == "PAUSED" and "CUMULATIVE_PAUSED" in result["reason_codes"]
    assert list(core.store.db.iterdump()) == before


def test_oversized_capability_inventory_keeps_full_and_compact_utf8_limits(core):
    result = snapshot(core.store, core.settings, core.clock)
    template = result["capabilities"][0]
    result["capabilities"] += [{**template, "id": "reading." + str(index) + "_" + "x" * 100}
                               for index in range(150)]
    for compact, limit in ((False, 6000), (True, 3000)):
        text = prompt(result, compact=compact)
        assert len(text.encode()) <= limit
        assert "未列出的能力不推定可用" in text


def test_voice_disabled_configuration_does_not_require_or_expose_signing_material(core):
    from companion.voice_config import VoiceConfig, VoiceNode

    core.settings.voice = VoiceConfig(enabled=False, nodes={
        "retained": VoiceNode(enabled=False, token="x" * 32, manifest_sha256="f" * 64),
    })
    result = rows(core)
    assert result["qq.voice.asr"]["state"] == result["qq.voice.tts"]["state"] == "UNAVAILABLE"
    assert "x" * 32 not in json.dumps(result)


def test_unknown_voice_resource_is_not_released_by_capability_projection(core, tmp_path):
    from companion.voice_jobs import VoiceJobs
    from tests.conftest import run
    from tests.sdk_reader_fixture import Provider, answer, worker_for
    from tests.support.voice_jobs import TEXT
    from tests.voice_owner_fixtures import native_voice

    native_voice(core, tmp_path, record=False)

    async def reply():
        async with worker_for(core, Provider(answer(TEXT))) as worker:
            assert await worker.run_once("capability-voice-reply")

    run(reply())
    message_id = core.store.one("SELECT message_id FROM outbox")["message_id"]
    jobs = VoiceJobs(core.store, core.settings, core.clock)
    with core.store.transaction():
        created = jobs.create_tts(message_id, TEXT)
        core.store.execute("UPDATE voice_jobs SET state='UNKNOWN',started_at=? WHERE job_id=?",
                           (core.clock(), created["job_id"]))
        core.store.insert("voice_node_state", node_id="local", active_job_id=created["job_id"],
                          resource_state="READY", last_seen_at=core.clock())
    before = list(core.store.db.iterdump())
    fact = rows(core)["qq.voice.tts"]
    assert fact["state"] == "PAUSED"
    assert "VOICE_RESOURCE_RECONCILIATION_REQUIRED" in fact["reason_codes"]
    assert list(core.store.db.iterdump()) == before

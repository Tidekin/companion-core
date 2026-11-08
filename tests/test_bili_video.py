"""Bili media keeps original Reader authority, immutable phases and native costs."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config

from companion import budget
from companion.bili_video import (
    _model_room,
    _policy,
    erase_bili_video,
    model_binding,
    restore_bili_video,
)
from companion.bili_video_protocol import (
    CAPABILITY,
    json_sha256,
)
from companion.cloud_budget import CloudBudgetConfig
from companion.contracts import CoreError
from companion.source_grants import SourceGrantSnapshot
from companion.store import Store, encode
from tests.conftest import run
from tests.postgres_fixture import logical_state
from tests.support.bili_video import (
    final_result,
    metadata,
    model_response,
    paid_media,
    phase_costs,
    phase_usage,
    preparation,
    receipt,
    upload_body,
)
from tests.support.historical_schema import historical_database
from tests.support.native_consumer import admit, select_tools
from tests.support.release_migration import one, open_historical, rows


def leased(core, **changes):
    module, item_id, source = metadata(core, **changes)
    request_id = module.schedule(item_id, source)
    item = core.reading.lease("bili", capabilities=[CAPABILITY])
    assert item and item["request_id"] == request_id
    return module, item


def prepared(core, **changes):
    module, item = leased(core, **changes)
    body = preparation(item)
    result = module.prepare("bili", item["request_id"], body)
    return module, item, body, result


def test_unbound_model_request_does_not_claim_legacy_media(core):
    assert model_binding(core.store, core.settings, core.clock, "unbound") is None


@pytest.mark.parametrize("current,state_mode,required_calls", [
    ("current", "original", 8), (None, "original", 9),
    ("unrelated", "original", 9), ("job-only", "original", 8),
    ("current", "all_unknown", 9), ("current", "settled_foreground", 8),
])
def test_model_room_counts_calls_once_and_preserves_foreground_slots(
    core, current, state_mode, required_calls,
):
    s, now = core.store, core.clock()
    rows = [
        ("job-only", "target", None, "memory_review", "SETTLED"),
        ("logical-only", "other", "target", "chat", "RESERVED"),
        ("overlap", "target", "target", "content_video_preview", "UNKNOWN"),
        ("null-job", None, "target", "content_video", "SETTLED"),
        ("other-admission", "target", "other", "content_video_preview", "CANCELLED"),
        ("same-fields", "target", None, "content_video_preview", "UNKNOWN"),
        ("current", "target", "target", "chat", "SETTLED"),
        ("unrelated", "other", "other", "memory_review", "SETTLED"),
    ]
    with s.transaction():
        for job_id in ("target", "other"):
            s.insert("jobs", job_id=job_id, kind="chat", state="RUNNING",
                     idempotency_key=job_id, created_at=now, updated_at=now)
        s.insert("model_budget_windows", window_id="headroom-window", contract_sha256="a" * 64)
        for request, job_id, logical, scenario, state in rows:
            if state_mode == "all_unknown":
                state = "UNKNOWN"
            elif state_mode == "settled_foreground" and scenario in {"chat", "memory_review"}:
                state = "SETTLED"
            s.insert("model_usage", usage_id="usage-" + request, request_id=request,
                     job_id=job_id, scenario=scenario, provider="synthetic", model="synthetic",
                     state=state, billed_micro_usd=123, occurred_at=now)
            if logical is not None:
                s.insert("model_admissions", request_id=request, logical_request_id=logical,
                         attempt_id="attempt-" + request, attempt_number=1,
                         window_id="headroom-window", contract_sha256="a" * 64,
                         payload_sha256="b" * 64, snapshot_json=encode({"synthetic": True}),
                         reserved_micro_usd=123, input_bound=10, output_bound=10,
                         lease_token="lease-" + request, lease_until=now,
                         journal_path="synthetic-not-opened", journal_id="synthetic")
        admission = s.one("SELECT * FROM model_admissions WHERE request_id='overlap'")
    admission.pop("record_sequence")
    with pytest.raises(psycopg.errors.NotNullViolation):
        s.insert("model_admissions", **{**admission, "request_id": None, "attempt_id": "null"})
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        s.insert(
            "model_admissions", **{**admission, "request_id": "absent", "attempt_id": "absent"}
        )
    before = {table: s.rows(f"SELECT * FROM {table} ORDER BY record_sequence")
              for table in ("model_usage", "model_admissions", "model_budget_windows")}
    changes = logical_state(s)
    for limit in range(1, 11):
        settings = SimpleNamespace(paid_model=SimpleNamespace(per_request_calls=limit))
        if limit < required_calls:
            with pytest.raises(CoreError, match="BILI_VIDEO_CHAT_HEADROOM_REQUIRED"):
                _model_room(s, settings, "target", current)
        else:
            _model_room(s, settings, "target", current)
    assert logical_state(s) == changes
    assert {table: s.rows(f"SELECT * FROM {table} ORDER BY record_sequence")
            for table in before} == before
    assert s.rows(
        "SELECT conname FROM pg_constraint WHERE connamespace=current_schema()::regnamespace "
        "AND contype='f' AND NOT convalidated") == []


def test_model_room_keeps_concurrent_reserved_call_visible_to_the_next_writer(core, tmp_path):
    now, ready = core.clock(), Barrier(2)

    def reserve(request):
        with closing(Store(core.store.database, core.store.state_directory)) as store:
            ready.wait(timeout=10)
            try:
                with store.transaction():
                    # model_binding rechecks this paid window under its native ledger lock.
                    _policy(store, core.settings, core.clock)
                    _model_room(store, core.settings, "target", request)
                    store.insert("model_usage", usage_id=request, request_id=request,
                                 job_id="target", scenario="content_video_preview",
                                 provider="synthetic", model="synthetic", state="RESERVED",
                                 billed_micro_usd=123, occurred_at=now)
            except CoreError as error:
                assert error.code == "BILI_VIDEO_CHAT_HEADROOM_REQUIRED"
                return "denied"
            return "reserved"

    async def scenario():
        async with paid_media(core, tmp_path, lambda _: pytest.fail("unexpected provider call"),
                              call_limit=4):
            core.store.insert("jobs", job_id="target", kind="chat", state="RUNNING",
                              idempotency_key="headroom-target", created_at=now, updated_at=now)
            core.store.insert("model_usage", usage_id="prior", request_id="prior", job_id="target",
                              scenario="content_video_preview", provider="synthetic",
                              model="synthetic", state="UNKNOWN", billed_micro_usd=123,
                              occurred_at=now)
            with ThreadPoolExecutor(max_workers=2) as workers:
                assert sorted(workers.map(reserve, ("first", "second"))) == ["denied", "reserved"]
            assert core.store.rows("SELECT state,billed_micro_usd FROM model_usage "
                                   "WHERE job_id='target' ORDER BY record_sequence") == [
                {"state": "UNKNOWN", "billed_micro_usd": 123},
                {"state": "RESERVED", "billed_micro_usd": 123},
            ]

    run(scenario())


def test_model_room_work_does_not_grow_with_unrelated_history(core, monkeypatch):
    s, now = core.store, core.clock()
    settings = SimpleNamespace(paid_model=SimpleNamespace(per_request_calls=4))
    with s.transaction():
        for job in ("target", "other"):
            s.insert("jobs", job_id=job, kind="chat", state="RUNNING",
                     idempotency_key=job, created_at=now, updated_at=now)
        s.insert("model_usage", usage_id="target", request_id="target", job_id="target",
                 scenario="content_video_preview", provider="synthetic", model="synthetic",
                 state="RESERVED", billed_micro_usd=123, occurred_at=now)

    def scanned_rows():
        visited = []
        query_rows = s.rows

        def inspect(query, parameters):
            plan = s.one("EXPLAIN (ANALYZE, FORMAT JSON) " + query, parameters)["QUERY PLAN"]
            nodes = [plan[0]["Plan"]]
            while nodes:
                node = nodes.pop()
                visited.append((node["Actual Rows"] + node.get("Rows Removed by Filter", 0))
                               * node["Actual Loops"])
                nodes.extend(node.get("Plans", []))
            return query_rows(query, parameters)

        with monkeypatch.context() as patch, s.read_snapshot():
            patch.setattr(s, "rows", inspect)
            _model_room(s, settings, "target")
        return sum(visited)

    initial = scanned_rows()
    s.execute(
        "INSERT INTO model_usage(usage_id, request_id, job_id, scenario, provider, model, "
        "state, billed_micro_usd, occurred_at) SELECT 'history-'||i, 'history-'||i, "
        "'other','chat','synthetic','synthetic', "
        "(ARRAY['SETTLED','RESERVED','UNKNOWN'])[1+mod(i,3)],123,%s FROM generate_series(1,1000) i",
        (now,),
    )
    s.execute("ANALYZE model_usage")
    assert scanned_rows() <= initial + 100


def test_disabled_video_requires_explicit_configuration_without_changing_metadata(core):
    module, item_id, source = metadata(core, enabled=False)
    with pytest.raises(CoreError, match="BILI_VIDEO_DISABLED"):
        module.schedule(item_id, source)
    assert core.store.one((
        'SELECT result_status FROM observations WHERE event_id=%s'
    ), (source,)) == {
        "result_status": "FETCHED"
    }
    assert not core.store.rows("SELECT * FROM bili_video_items")


def test_native_child_requires_capability_and_preserves_original_reading_item(core):
    module, item_id, source = metadata(core)
    original = core.store.one(
        'SELECT read_request_id FROM content_items WHERE item_id=%s', (item_id,)
    )
    child = module.schedule(item_id, source)
    assert module.schedule(item_id, source) == child
    assert child != original["read_request_id"]
    assert core.reading.lease("bili") is None
    item = core.reading.lease("bili", capabilities=[CAPABILITY])
    assert item["request_id"] == child
    assert item["max_bytes"] == 32 * 1024 * 1024 + 1024 * 1024
    assert item["max_requests"] == 8 and item["max_items"] == 1
    native = core.store.one('SELECT * FROM exploration_requests WHERE request_id=%s', (child,))
    assert native["reserved_bytes"] == item["max_bytes"]
    assert native["reserved_requests"] == item["max_requests"]
    assert native["lease_until"] == item["media"]["deadline"]
    assert (
        core.store.one('SELECT read_request_id FROM content_items WHERE item_id=%s', (item_id,))
        == original
    )
    assert not core.store.rows("SELECT * FROM model_usage")


def test_prepare_freezes_exact_playinfo_duration_and_only_bounded_native_selection(core):
    module, item, request, result = prepared(core)
    assert result["target"]["duration_seconds"] == 60.04
    assert result["prepare_sha256"] == json_sha256(request)
    assert 1 <= len(result["selection"]["frame_times"]) <= 4
    assert all(0 <= at < 60.04 for at in result["selection"]["frame_times"])
    assert result["selection"]["clips"] == []
    row = core.store.one("SELECT * FROM bili_video_items")
    assert row["state"] == "PREPARED"
    assert json.loads(row["metadata_json"])["duration_seconds"] == 60
    assert json.loads(row["target_json"])["duration_seconds"] == 60.04
    plan = json.loads(row["plan_json"])
    assert (
        sum(segment["end_seconds"] - segment["start_seconds"] for segment in plan["segments"]) <= 30
    )
    assert module.receipt("bili", item["request_id"], receipt(item, "prepare", request)) == {
        "state": "FOUND",
        "response": result,
    }
    assert not core.store.rows("SELECT * FROM model_usage")


@pytest.mark.parametrize("grant_seconds", [None, 10])
def test_account_media_lease_respects_content_and_grant_deadlines(core, grant_seconds):
    from source_reader.protocol import decode_lease

    expiry = core.clock.now + timedelta(seconds=grant_seconds) if grant_seconds else None
    grant = SourceGrantSnapshot(grant_id="grant_" + "a" * 32, epoch=2,
                                fingerprint="b" * 64, expires_at=expiry)
    cutoff = core.clock.now + timedelta(seconds=20)
    module, item_id, source = metadata(core, deadline=cutoff.isoformat(), grant=grant)
    request = module.schedule(item_id, source)
    item = core.reading.lease("bili", grant, capabilities=[CAPABILITY])
    assert item["request_id"] == request and item["source_grant"] == grant.snapshot()
    _, _, _, until = decode_lease({"item": item}, "bilibili", core.clock.now,
                                  media_limits=core.settings.bili_video.media)
    assert until == (expiry or cutoff)


def test_offset_content_deadline_is_normalized_before_comparing_to_utc_policy(core):
    cutoff = core.clock.now + timedelta(seconds=20)
    local = cutoff.astimezone(timezone(timedelta(hours=8))).isoformat(timespec="microseconds")
    module, item = leased(core, deadline=local)
    assert item["media"]["deadline"] == cutoff.isoformat(timespec="microseconds")
    core.clock.advance(21)
    with pytest.raises(CoreError, match="BILI_VIDEO_SOURCE_CHANGED"):
        module.prepare("bili", item["request_id"], preparation(item))


@pytest.mark.parametrize(
    "path,value",
    [
        ("identity.cid", 124),
        ("identity.part", True),
        ("identity.duration_seconds", 62),
        ("proof.duration_milliseconds", True),
        ("proof.resolver", "guessed"),
        ("proof.playinfo_response_sha256", "bad"),
        ("proof.width", True),
        ("proof.declared_bytes", 40_000_000),
    ],
)
def test_prepare_rejects_unbound_or_unbounded_source_without_phase_or_cost(core, path, value):
    module, item = leased(core)
    body = preparation(item)
    left, key = path.split(".")
    body[left][key] = value
    with pytest.raises(CoreError, match="BILI_VIDEO_PREPARE_INVALID"):
        module.prepare("bili", item["request_id"], body)
    assert not core.store.rows("SELECT * FROM bili_video_phases")
    assert not core.store.rows("SELECT * FROM model_usage")


def test_lost_prepare_ack_uses_readonly_receipt_after_expiry_without_second_prepare(core):
    module, item, body, result = prepared(core)
    with pytest.raises(CoreError, match="BILI_VIDEO_RECEIPT_REQUIRED"):
        module.prepare("bili", item["request_id"], body)
    core.clock.advance(400)
    assert module.receipt("bili", item["request_id"], receipt(item, "prepare", body)) == {
        "state": "FOUND",
        "response": result,
    }
    wrong = receipt(item, "prepare", body) | {"request_sha256": "0" * 64}
    assert module.receipt("bili", item["request_id"], wrong) == {"state": "NOT_FOUND"}
    assert len(core.store.rows("SELECT * FROM bili_video_phases")) == 1


@pytest.mark.parametrize(
    "mutation", ["wrong_selection", "wrong_prepare", "bytes", "input", "range", "url"]
)
def test_upload_rejects_modified_materialization_before_any_paid_reservation(core, mutation):
    module, item, _, result = prepared(core)
    body = upload_body(item, result)
    if mutation == "wrong_selection":
        body["selection_sha256"] = "0" * 64
    elif mutation == "wrong_prepare":
        body["prepare_sha256"] = "0" * 64
    elif mutation == "bytes":
        body["assets"][0]["base64"] = "AAAA"
    elif mutation == "input":
        body["download"]["input_sha256"] = "a" * 64
    elif mutation == "range":
        body["download"]["source_range"]["start_seconds"] = True
    else:
        body["download"]["url"] = "https://unapproved.invalid/video"
    with pytest.raises(CoreError, match="BILI_VIDEO_UPLOAD_INVALID"):
        run(module.upload("bili", item["request_id"], body))
    assert not core.store.rows("SELECT * FROM model_usage")
    assert core.store.one("SELECT COUNT(*) n FROM bili_video_phases")["n"] == 1


def test_upload_cannot_hold_an_outer_transaction_across_provider_await(core):
    module, item, _, result = prepared(core)
    with core.store.transaction(), pytest.raises(RuntimeError, match="NO_TRANSACTION_ACROSS_MODEL"):
        run(module.upload("bili", item["request_id"], upload_body(item, result)))
    assert not core.store.rows("SELECT * FROM model_usage")


def test_late_erased_reader_receipt_settles_only_original_metering(core):
    module, item, request, result = prepared(core)
    with core.store.transaction():
        erase_bili_video(core.store, [item["media"]["source_event_id"]])
    body = final_result(item, result)
    assert module.result("bili", item["request_id"], body) == {"accepted": True}
    attempt = core.store.one(
        'SELECT * FROM reading_attempts WHERE request_id=%s', (item["request_id"],)
    )
    assert attempt["state"] == "CANCELLED" and attempt["observed_bytes"] == 1500
    assert attempt["context_json"] == "{}" and attempt["result_event_ids_json"] == "[]"
    assert module.context(item["media"]["content_item_id"]) is None
    with pytest.raises(CoreError, match="BILI_VIDEO_SOURCE_ERASED"):
        module.receipt("bili", item["request_id"], receipt(item, "prepare", request))
    assert module.result("bili", item["request_id"], body) == {"accepted": True}
    with pytest.raises(CoreError, match="IDEMPOTENCY_CONFLICT"):
        module.result("bili", item["request_id"], body | {"error_code": "DIFFERENT"})


def test_overrun_receipt_retains_observed_usage_and_unknown_reservation(core):
    module, item, _, result = prepared(core)
    over = item["max_bytes"] + 1
    module.result("bili", item["request_id"], final_result(item, result, used_bytes=over))
    native = core.store.one(
        'SELECT * FROM exploration_requests WHERE request_id=%s', (item["request_id"],)
    )
    attempt = core.store.one(
        'SELECT * FROM reading_attempts WHERE request_id=%s', (item["request_id"],)
    )
    assert native["state"] == "UNKNOWN" and native["used_bytes"] is None
    assert native["reserved_bytes"] == item["max_bytes"]
    assert attempt["observed_bytes"] == over


def test_restore_fences_pending_native_work_without_clearing_its_reservations(core):
    module, item, _, _ = prepared(core)
    native = core.store.one(
        'SELECT * FROM exploration_requests WHERE request_id=%s', (item["request_id"],)
    )
    with core.store.transaction():
        restore_bili_video(core.store, core.clock())
    row = core.store.one(
        'SELECT * FROM exploration_requests WHERE request_id=%s', (item["request_id"],)
    )
    assert row["state"] == "UNKNOWN" and row["reserved_bytes"] == native["reserved_bytes"]
    with pytest.raises(CoreError, match="BILI_VIDEO_RECONCILIATION_REQUIRED"):
        module.prepare("bili", item["request_id"], preparation(item))


def test_migration_preserves_existing_data_and_refuses_downgrade_with_media_history(core, tmp_path):
    _, item, _, _ = prepared(core)
    database = historical_database(core.store, tmp_path / "bili.db", "0032_bili_video")
    with closing(open_historical(database)) as prior:
        before = rows(prior, "SELECT * FROM reading_attempts")
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    config.set_main_option(
        "sqlalchemy.url", "sqlite:///" + database.as_posix()
    )
    with pytest.raises(RuntimeError, match="BILI_VIDEO_DOWNGRADE_REQUIRES_EXPLICIT_DATA_REVIEW"):
        command.downgrade(config, "0031_voice_nodes")
    with closing(open_historical(database)) as store:
        assert rows(store, "SELECT * FROM reading_attempts") == before
        assert one(store, "SELECT state FROM bili_video_items WHERE item_id=?",
                   (item["media"]["content_item_id"],))["state"] == "PREPARED"


@pytest.mark.parametrize("need_clips", [False, True])
def test_paid_preview_and_optional_clips_share_native_video_task_and_partial_evidence(
    core,
    tmp_path,
    need_clips,
):
    calls = []

    def provider(request):
        value = json.loads(request.content)
        calls.append((str(request.url), value))
        return model_response(
            {"analysis": "局部样本中有蓝色画面", "need_clips": need_clips}
            if len(calls) == 1
            else {"analysis": "指定片段里蓝色画面保持静止"}
        )

    async def scenario():
        async with paid_media(core, tmp_path, provider) as (module, item, prepared):
            root = core.store.one(
                'SELECT * FROM events WHERE event_id=%s', (item["media"]["source_event_id"],)
            )
            preview_body = upload_body(item, prepared)
            preview = await module.upload("bili", item["request_id"], preview_body)
            phases = [preview]
            if need_clips:
                assert preview["next_selection"] is not None
                clip_body = upload_body(item, prepared, preview["next_selection"])
                phases.append(await module.upload("bili", item["request_id"], clip_body))
            else:
                assert preview["next_selection"] is None
            assert module.receipt(
                "bili", item["request_id"], receipt(item, "preview", preview_body)
            ) == {"state": "FOUND", "response": preview}
            module.result(
                "bili", item["request_id"], final_result(item, prepared, phases, status="SUCCESS")
            )
            result = module.context(item["media"]["content_item_id"])
            assert result["state"] == "COMPLETE" and result["result_status"] == "FETCHED"
            assert result["partial"] is True and result["complete_video"] is False
            assert result["audio_analyzed"] is False
            assert result["evidence"]["visual_observed"] is True
            assert len(result["evidence"]["phases"]) == len(phases)
            assert (
                core.store.one((
                    'SELECT * FROM events WHERE event_id=%s'
                ), (root["event_id"],)) == root
            )
            output = core.store.one(
                'SELECT * FROM observations WHERE event_id=%s', (result["event_id"],)
            )
            assert output["source_kind"] == "text" and output["result_status"] == "FETCHED"
            usage = phase_usage(core)
            assert [row["scenario"] for row in usage] == (
                ["content_video_preview", "content_video"]
                if need_clips
                else ["content_video_preview"]
            )
            assert all(
                row["state"] == "SETTLED" and row["billed_micro_usd"] == 140 for row in usage
            )
            costs = phase_costs(core)
            assert len(costs) == len(phases)
            assert {row["task_id"] for row in costs} == {
                "content:" + item["media"]["content_item_id"]
            }
            assert {row["task_kind"] for row in costs} == {"video"}
            assert len({row["job_id"] for row in usage}) == 1
            assert core.store.one("SELECT COUNT(*) n FROM outbox")["n"] == 0
            for row in core.store.rows(
                "SELECT evidence_json,analysis_json,response_json FROM bili_video_phases"
            ):
                assert "base64" not in json.dumps(row) and "data:image" not in json.dumps(row)
            with pytest.raises(CoreError):
                await module.upload("bili", item["request_id"], preview_body)
            assert phase_usage(core) == usage

    run(scenario())
    assert len(calls) == (2 if need_clips else 1)
    assert all(url == "https://video.example.test/v1/chat/completions" for url, _ in calls)
    assert all(body["model"] == "synthetic-bili-video" and "tools" not in body for _, body in calls)
    assert calls[0][1]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("kind", ["daily", "task"])
def test_cloud_zero_limit_rejects_before_provider_and_native_charge(core, tmp_path, kind):
    calls = []
    cloud = CloudBudgetConfig(
        **{"daily_micro_cny" if kind == "daily" else "video_per_item_micro_cny": 0}
    )

    def provider(request):
        calls.append(request)
        return model_response({"analysis": "局部蓝色画面", "need_clips": False})

    async def scenario():
        async with paid_media(core, tmp_path, provider, cloud=cloud) as (module, item, prepared):
            with pytest.raises(CoreError, match="CLOUD_.*BUDGET_EXHAUSTED"):
                await module.upload("bili", item["request_id"], upload_body(item, prepared))
            assert not calls
            assert not phase_usage(core)
            assert not phase_costs(core)

    run(scenario())


def test_paid_invalid_proposal_keeps_known_fee_without_making_observation(core, tmp_path):
    async def scenario():
        async with paid_media(
            core,
            tmp_path,
            lambda _: model_response(
                {
                    "analysis": "伪造的扩大请求",
                    "need_clips": True,
                    "url": "https://unapproved.invalid",
                }
            ),
        ) as (module, item, prepared):
            with pytest.raises(CoreError, match="BILI_VIDEO_ANALYSIS_INVALID"):
                await module.upload("bili", item["request_id"], upload_body(item, prepared))
            usage = phase_usage(core)[0]
            assert usage["state"] == "SETTLED" and usage["billed_micro_usd"] == 140
            assert (
                core.store.one("SELECT output_event_id FROM bili_video_items")["output_event_id"]
                is None
            )
            assert (
                core.store.one("SELECT state FROM bili_video_phases WHERE phase='preview'")["state"]
                == "REJECTED"
            )

    run(scenario())


def test_lost_model_response_retains_unknown_cost_and_forbids_upload_repayment(core, tmp_path):
    calls = []

    def provider(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic unknown response")

    async def scenario():
        async with paid_media(core, tmp_path, provider) as (module, item, prepared):
            body = upload_body(item, prepared)
            with pytest.raises(CoreError, match="MODEL_RETRYABLE"):
                await module.upload("bili", item["request_id"], body)
            usage = phase_usage(core)[0]
            assert usage["state"] == "UNKNOWN" and usage["billed_micro_usd"] > 0
            assert module.receipt("bili", item["request_id"], receipt(item, "preview", body)) == {
                "state": "PENDING"
            }
            with pytest.raises(CoreError):
                await module.upload("bili", item["request_id"], body)
            assert len(calls) == 1
            assert phase_usage(core) == [usage]
            module.result(
                "bili",
                item["request_id"],
                final_result(item, prepared, status="UNKNOWN", used_requests=0, used_bytes=0),
            )
            meter = core.store.one(
                'SELECT * FROM reading_attempts WHERE request_id=%s', (item["request_id"],)
            )
            assert meter["state"] == "UNKNOWN"
            assert meter["observed_requests"] == 4 and meter["observed_bytes"] == 1000

    run(scenario())


def test_source_erased_during_paid_await_keeps_fee_and_cannot_resurrect_analysis(core, tmp_path):
    def provider(_):
        source = core.store.one("SELECT source_event_id FROM bili_video_items")["source_event_id"]
        with core.store.transaction(control_write=True):
            core.ledger.expire_events(core.store, [source], core.clock())
        return model_response({"analysis": "这段结果必须因擦除丢弃", "need_clips": False})

    async def scenario():
        async with paid_media(core, tmp_path, provider) as (module, item, prepared):
            with pytest.raises(CoreError):
                await module.upload("bili", item["request_id"], upload_body(item, prepared))
            usage = phase_usage(core)[0]
            assert usage["state"] == "SETTLED" and usage["billed_micro_usd"] == 140
            row = core.store.one("SELECT * FROM bili_video_items")
            assert row["redacted"] == 1 and row["output_event_id"] is None
            assert row["lease_json"] == row["target_json"] == row["plan_json"] == "{}"
            assert module.context(row["item_id"]) is None
            assert (
                core.store.one(
                    "SELECT COUNT(*) n FROM bili_video_phases WHERE analysis_json<>'{}'"
                )["n"]
                == 0
            )
            assert not core.store.rows("SELECT * FROM outbox")

    run(scenario())


def test_offset_live_window_is_the_shortest_child_deadline(core, tmp_path):
    async def scenario():
        async with paid_media(core, tmp_path, lambda _: model_response({}), short_window=True) as (
            _,
            item,
            _,
        ):
            from companion import owner_authority

            expected = owner_authority.current(core.store, core.settings, core.clock,
                purpose="bili_video").policy.expires_at.astimezone(UTC)
            assert datetime.fromisoformat(item["media"]["deadline"]) == expected
            assert expected == core.clock.now + timedelta(seconds=45)

    run(scenario())


def test_clip_keeps_preview_and_clip_charges_above_item_currency_amount(core, tmp_path):
    calls = []

    def provider(request):
        calls.append(request)
        return model_response(
            {"analysis": "需要检查预先定义的片段", "need_clips": True}
            if len(calls) == 1 else {"analysis": "选定片段的局部画面"}, tokens=18000
        )

    async def scenario():
        cloud = CloudBudgetConfig(video_per_item_micro_cny=150000)
        async with paid_media(core, tmp_path, provider, cloud=cloud) as (module, item, prepared):
            preview = await module.upload("bili", item["request_id"], upload_body(item, prepared))
            assert preview["next_selection"] is not None
            cost = core.store.one("SELECT * FROM cloud_cost_snapshots WHERE task_kind='video'")
            assert cost["accounted_micro_usd"] == 18040
            assert cost["task_id"] == "content:" + item["media"]["content_item_id"]
            assert 18040 * 8 < cloud.video_per_item_micro_cny
            await module.upload(
                "bili", item["request_id"], upload_body(item, prepared, preview["next_selection"]))
            assert len(calls) == 2
            assert len(phase_usage(core)) == 2
            assert phase_costs(core)[0] == cost
            assert sum(row["amount_micro_cny"] for row in phase_costs(core)) > (
                cloud.video_per_item_micro_cny)
            partial = module.context(item["media"]["content_item_id"])
            assert partial["result_status"] == "FETCHED"
            assert [phase["phase"] for phase in partial["evidence"]["phases"]] == [
                "preview", "clips"]

    run(scenario())


def test_native_budget_rejects_changed_model_payload_before_any_reservation(
    core, tmp_path, monkeypatch
):
    original_reserve = budget.reserve

    def change_payload(store, settings, job_id, scenario, messages, now, **kwargs):
        if scenario not in {"content_video_preview", "content_video"}:
            return original_reserve(store, settings, job_id, scenario, messages, now, **kwargs)
        changed = json.loads(json.dumps(messages))
        changed[0]["content"][0]["text"] += "被替换的请求"
        return original_reserve(store, settings, job_id, scenario, changed, now, **kwargs)

    monkeypatch.setattr(budget, "reserve", change_payload)
    calls = []

    def provider(request):
        calls.append(request)
        return model_response({"analysis": "局部蓝色画面", "need_clips": False})

    async def scenario():
        async with paid_media(core, tmp_path, provider) as (module, item, prepared):
            with pytest.raises(CoreError, match="BILI_VIDEO_MODEL_BINDING_CHANGED"):
                await module.upload("bili", item["request_id"], upload_body(item, prepared))
            assert not calls
            assert not phase_usage(core)
            assert not phase_costs(core)

    run(scenario())


def test_prepare_receipt_cannot_be_followed_by_a_smaller_settled_request_count(core):
    module, item, _, preparation_result = prepared(core)
    body = final_result(item, preparation_result, used_requests=2)
    with pytest.raises(CoreError, match="BILI_VIDEO_RESULT_INVALID"):
        module.result("bili", item["request_id"], body)
    meter = core.store.one(
        'SELECT * FROM reading_attempts WHERE request_id=%s', (item["request_id"],)
    )
    assert meter["state"] == "RUNNING" and meter["observed_requests"] == 3
    assert meter["result_sha256"] is None


def test_known_receipt_cannot_reduce_the_already_received_download_meter(core, tmp_path):
    async def scenario():
        async with paid_media(
            core,
            tmp_path,
            lambda _: model_response({"analysis": "局部蓝色画面", "need_clips": False}),
        ) as (module, item, prepared):
            preview = await module.upload("bili", item["request_id"], upload_body(item, prepared))
            meter = core.store.one(
                'SELECT * FROM reading_attempts WHERE request_id=%s', (item["request_id"],)
            )
            assert meter["observed_requests"] == 4 and meter["observed_bytes"] == 1000
            for counts in ({"used_requests": 3}, {"used_bytes": 999}):
                body = final_result(item, prepared, [preview], status="SUCCESS", **counts)
                with pytest.raises(CoreError, match="BILI_VIDEO_RESULT_INVALID"):
                    module.result("bili", item["request_id"], body)
                assert (
                    core.store.one(
                        'SELECT * FROM reading_attempts WHERE request_id=%s', (item["request_id"],)
                    )
                    == meter
                )
            assert module.result(
                "bili",
                item["request_id"],
                final_result(item, prepared, [preview], status="SUCCESS"),
            ) == {"accepted": True}

    run(scenario())


@pytest.mark.parametrize("when", ["metadata", "before_preview", "between_stages"])
def test_exact_followup_binds_only_new_phases_to_current_consumer(core, tmp_path, when):
    calls = []

    def provider(request):
        calls.append(request)
        return model_response(
            {"analysis": "局部样本呈现蓝色画面", "need_clips": when == "between_stages"}
            if len(calls) == 1
            else {"analysis": "指定片段仍是蓝色画面"}
        )

    async def scenario():
        controls = {}
        async with paid_media(
            core, tmp_path, provider, controls=controls, followup_before_metadata=when == "metadata"
        ) as (module, item, prepared):
            root = core.store.one("SELECT * FROM bili_video_items")
            original_job = core.store.one(
                'SELECT job_id FROM jobs WHERE event_id=%s', (root["owner_event_id"],)
            )["job_id"]

            async def followup(text="继续读一下"):
                window = controls["window"]
                admitted = await admit(controls["client"], core, window, 2, text)
                await select_tools(core, "analyze_shared_video", [item["url"]])
                return admitted

            if when == "before_preview":
                await followup()
            preview_body = upload_body(item, prepared)
            preview = await module.upload("bili", item["request_id"], preview_body)
            before = core.store.one("SELECT * FROM bili_video_phases WHERE phase='preview'")
            results = [preview]
            if when == "between_stages":
                await followup()
                results.append(
                    await module.upload(
                        "bili",
                        item["request_id"],
                        upload_body(item, prepared, preview["next_selection"]),
                    )
                )
                assert (
                    core.store.one("SELECT * FROM bili_video_phases WHERE phase='preview'")
                    == before
                )
                assert before["model_job_id"] == original_job
            consumer = core.store.one(
                "SELECT j.* FROM jobs j JOIN events e USING(event_id) "
                "JOIN conversations c USING(conversation_id) "
                "WHERE j.kind='chat' AND e.reply_revision=c.reply_revision"
            )
            assert consumer["job_id"] != original_job
            phases = core.store.rows(
                "SELECT * FROM bili_video_phases WHERE phase<>'prepare' "
                "ORDER BY created_at,phase DESC"
            )
            assert phases[-1]["model_job_id"] == consumer["job_id"]
            assert core.store.one("SELECT job_id FROM bili_video_items")["job_id"] == root["job_id"]
            usage = phase_usage(core)
            assert [row["job_id"] for row in usage] == [phase["model_job_id"] for phase in phases]
            assert all(row["state"] == "SETTLED" for row in usage)
            assert {
                row["task_id"] for row in phase_costs(core)
            } == {"content:" + root["item_id"]}
            module.result(
                "bili", item["request_id"], final_result(item, prepared, results, status="SUCCESS")
            )
            result = module.context(root["item_id"])
            sources = {
                row["source_event_id"]
                for row in core.store.rows(
                    'SELECT source_event_id FROM event_dependencies WHERE event_id=%s',
                    (result["event_id"],),
                )
            }
            assert {
                root["owner_event_id"],
                root["source_event_id"],
                consumer["event_id"],
            } <= sources
            with pytest.raises(CoreError):
                await module.upload("bili", item["request_id"], preview_body)
            assert len(calls) == len(usage)
            with core.store.transaction(control_write=True):
                core.ledger.expire_events(core.store, [consumer["event_id"]], core.clock())
            assert module.context(root["item_id"]) is None
            assert core.store.one(
                'SELECT redacted FROM events WHERE event_id=%s', (result["event_id"],)
            ) == {"redacted": 1}
            assert phase_usage(core) == usage

    run(scenario())


def test_followup_arriving_during_model_cannot_move_or_repay_started_phase(core, tmp_path):
    controls, calls = {}, []

    async def provider(request):
        calls.append(request)
        window = controls["window"]
        _, admitted = await admit(controls["client"], core, window, 2, "继续读一下")
        from companion.core import Core

        with Core(core.settings, clock=core.clock) as independent:
            assert independent.store is not core.store
            assert independent.claim_job("wait-for-original-preview") is None
            assert independent.store.one('SELECT state FROM jobs WHERE job_id=%s',
                (admitted["job_id"],)) == {"state": "QUEUED"}
        return model_response({"analysis": "旧阶段返回的局部结果", "need_clips": False})

    async def scenario():
        async with paid_media(core, tmp_path, provider, controls=controls) as (
            module,
            item,
            prepared,
        ):
            initial_job = core.store.one("SELECT job_id FROM bili_video_items")["job_id"]
            body = upload_body(item, prepared)
            with pytest.raises(CoreError, match="BILI_VIDEO_OWNER_SOURCE_REQUIRED"):
                await module.upload("bili", item["request_id"], body)
            phase = core.store.one("SELECT * FROM bili_video_phases WHERE phase='preview'")
            usage = phase_usage(core)[0]
            assert phase["model_job_id"] == usage["job_id"] == initial_job
            assert usage["state"] == "SETTLED" and usage["billed_micro_usd"] == 140
            assert phase["state"] == "REJECTED"
            assert (
                core.store.one("SELECT output_event_id FROM bili_video_items")["output_event_id"]
                is None
            )
            with pytest.raises(CoreError):
                await module.upload("bili", item["request_id"], body)
            assert len(calls) == 1 and phase_usage(core) == [usage]
            await select_tools(core, "analyze_shared_video", [controls["url"]])
            assert core.store.one("SELECT * FROM bili_video_phases WHERE phase='preview'") == phase
            with pytest.raises(CoreError):
                await module.upload("bili", item["request_id"], body)
            assert len(calls) == 1 and phase_usage(core) == [usage]

    run(scenario())


@pytest.mark.parametrize(
    "text,reference", [("先聊别的", None), ("不用继续看了", None), ("解释这里", 999)]
)
def test_unrelated_or_negative_followup_cannot_authorize_new_phase(core, tmp_path, text, reference):
    async def scenario():
        controls = {}
        async with paid_media(core, tmp_path, lambda _: model_response({}), controls=controls) as (
            module,
            item,
            prepared,
        ):
            window = controls["window"]
            await admit(
                controls["client"],
                core,
                window,
                2,
                text,
                reference=reference,
            )
            with pytest.raises(CoreError, match="BILI_VIDEO_OWNER_SOURCE_REQUIRED"):
                await module.upload("bili", item["request_id"], upload_body(item, prepared))
            assert not phase_usage(core)
            assert not core.store.rows("SELECT * FROM bili_video_phases WHERE phase='preview'")

    run(scenario())


def test_erasing_new_consumer_during_await_scrubs_its_phase_without_erasing_original_source(
    core, tmp_path
):
    controls = {}

    def provider(_):
        with core.store.transaction(control_write=True):
            core.ledger.expire_events(core.store, [controls["consumer_id"]], core.clock())
        return model_response({"analysis": "消费者已经擦除的结果", "need_clips": False})

    async def scenario():
        async with paid_media(core, tmp_path, provider, controls=controls) as (
            module,
            item,
            prepared,
        ):
            source = core.store.one(
                'SELECT * FROM events WHERE event_id=%s', (item["media"]["source_event_id"],)
            )
            window = controls["window"]
            consumer, _ = await admit(
                controls["client"], core, window, 2, "继续读一下"
            )
            controls["consumer_id"] = consumer["event_id"]
            await select_tools(core, "analyze_shared_video", [item["url"]])
            with pytest.raises(CoreError):
                await module.upload("bili", item["request_id"], upload_body(item, prepared))
            usage = phase_usage(core)[0]
            assert usage["state"] == "SETTLED" and usage["billed_micro_usd"] == 140
            phase = core.store.one("SELECT * FROM bili_video_phases WHERE phase='preview'")
            assert (
                phase["analysis_json"] == phase["evidence_json"] == phase["response_json"] == "{}"
            )
            assert core.store.one("SELECT redacted,output_event_id FROM bili_video_items") == {
                "redacted": 1,
                "output_event_id": None,
            }
            retained = core.store.one(
                'SELECT * FROM events WHERE event_id=%s', (source["event_id"],)
            )
            assert retained["redacted"] == 0 and retained["body_json"] == source["body_json"]

    run(scenario())


@pytest.mark.parametrize("call_limit,media_calls", [(4, 2), (5, 3), (6, 4)])
def test_two_video_inputs_keep_foreground_call_room_and_still_produce_a_reply(
    core,
    tmp_path,
    call_limit,
    media_calls,
):
    calls = []

    def provider(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["model"] != "synthetic-bili-video":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "我结合两份原始字幕和已经检查的局部画面来回答。"}}
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                },
            )
        parts = body["messages"][0]["content"]
        is_preview = parts[1]["type"] == "image_url"
        return model_response(
            {"analysis": "局部样本可见蓝色画面", "need_clips": True}
            if is_preview
            else {"analysis": "选定片段中的蓝色画面保持静止"}
        )

    async def scenario():
        async with paid_media(core, tmp_path, provider, two_links=True, call_limit=call_limit) as (
            module,
            first,
            first_prepared,
        ):
            original_job = core.store.one("SELECT job_id FROM bili_video_items LIMIT 1")["job_id"]

            async def complete(item, prepared):
                results = [
                    await module.upload("bili", item["request_id"], upload_body(item, prepared))
                ]
                if results[0]["next_selection"] is not None:
                    results.append(
                        await module.upload(
                            "bili",
                            item["request_id"],
                            upload_body(item, prepared, results[0]["next_selection"]),
                        )
                    )
                module.result(
                    "bili",
                    item["request_id"],
                    final_result(item, prepared, results, status="SUCCESS"),
                )
                return results

            assert len(await complete(first, first_prepared)) == (1 if call_limit == 4 else 2)
            second = core.reading.lease("bili", capabilities=[CAPABILITY])
            assert second is not None and second["request_id"] != first["request_id"]
            second_prepared = module.prepare("bili", second["request_id"], preparation(second))
            if call_limit <= 5:
                with pytest.raises(CoreError, match="BILI_VIDEO_CHAT_HEADROOM_REQUIRED"):
                    await complete(second, second_prepared)
                module.result("bili", second["request_id"], final_result(second, second_prepared))
            else:
                results = await complete(second, second_prepared)
                assert len(results) == 1
                partial = module.context(second["media"]["content_item_id"])
                assert len(partial["evidence"]["phases"]) == len(results)
            usage = phase_usage(core)
            # The native SDK has spent two selection calls and still reserves its final reply.
            assert len(usage) == media_calls - 1
            assert call_limit - len(core.store.rows("SELECT * FROM model_usage")) >= 1
            assert {row["job_id"] for row in usage} == {original_job}
            costs = phase_costs(core)
            assert len({row["task_id"] for row in costs}) == (1 if call_limit <= 5 else 2)
            assert await core.run_once()
            assert (
                core.store.one('SELECT state FROM jobs WHERE job_id=%s', (original_job,))["state"]
                == "SUCCEEDED"
            )
            assert (
                core.store.one("SELECT COUNT(*) n FROM model_usage WHERE scenario='chat'")["n"] == 3
            )
            reply = core.store.one(
                "SELECT state,content_json FROM outbox WHERE action_type='qq.private.reply'"
            )
            assert reply["state"] == "PENDING" and "两份原始字幕" in reply["content_json"]
            assert (
                core.store.one(
                    'SELECT COUNT(*) n FROM model_admissions WHERE logical_request_id=%s',
                    (original_job,),
                )["n"]
                <= call_limit
            )

    run(scenario())

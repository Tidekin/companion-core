"""Native custody failures never discard UNKNOWN money, source or execution history."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from companion import model_carry, owner_authority, owner_input_state, paid_budget
from companion.api import create_app
from companion.contracts import CoreError
from companion.store import encode
from companion.transport import enqueue
from companion_delivery_protocol import digest
from tests.model_carry_support import stranded


def test_authenticated_receipt_is_identical_before_and_after_successor(core, tmp_path):
    from tests.support.owner_authority import activate

    case = stranded(core, tmp_path)
    receipt = model_carry.prepare(
        core.store, core.settings, case.request, case.verified, core.clock()
    )
    path = "/internal/v1/owner-authority/model-carry-receipt"
    body = {
        "authority": case.active["authority"],
        "carry_sha256": receipt["carry_sha256"],
        "successor_policy_sha256": receipt["successor_policy_sha256"],
    }
    headers = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
    with TestClient(create_app(core.settings)) as client:
        assert client.post(path, json=body).status_code == 401
        original = client.post(path, json=body, headers=headers)
        assert original.status_code == 200, original.text
        assert original.json() == receipt and original.headers["Cache-Control"] == "no-store"
        assert (
            client.post(
                path, json=body | {"successor_policy_sha256": "0" * 64}, headers=headers
            ).status_code
            == 409
        )
        activate(core, case.policy, revision=2, operation="activate-api-carry")
        assert client.post(path, json=body, headers=headers).json() == receipt


def test_carry_cannot_omit_even_one_unknown(core, tmp_path):
    case = stranded(core, tmp_path)
    with core.store.transaction():
        view = owner_authority.custody(core.store, core.settings, settlement=True)
        value = model_carry.manifest(
            core.store,
            core.settings,
            case.active["authority"],
            case.request["successor_window_id"],
            case.verified.value,
            view,
        )
    assert len(value["unknowns"]) == 1
    omitted = value | {"unknowns": []}
    case.request["manifest_sha256"] = digest(omitted)
    case.request["successor_policy"]["unknown_carry_sha256"] = digest(omitted)
    with pytest.raises(CoreError, match="OWNER_MODEL_CARRY_CHANGED"):
        model_carry.prepare(core.store, core.settings, case.request, case.verified, core.clock())
    assert core.store.one("SELECT state FROM model_usage")["state"] == "UNKNOWN"


def test_pending_delivery_cannot_be_carried(core, tmp_path):
    case = stranded(core, tmp_path)
    source = core.store.one(
        "SELECT conversation_id FROM events WHERE event_id=?", (case.capture.event_id,)
    )
    pending = enqueue(
        core.store,
        case.capture.event_id,
        source["conversation_id"],
        "synthetic unresolved delivery",
        core.clock(),
    )
    before = core.store.rows("SELECT * FROM outbox")
    with pytest.raises(CoreError, match="OWNER_MODEL_CARRY_NOT_QUIESCENT"):
        model_carry.prepare(core.store, core.settings, case.request, case.verified, core.clock())
    assert core.store.rows("SELECT * FROM outbox") == before
    assert pending is not None


def test_started_animation_marker_is_retained_as_evidence(core, tmp_path):
    case = stranded(core, tmp_path)
    core.store.insert(
        "agent_tool_calls",
        job_id=case.job_id,
        tool_call_id="old-motion-call",
        tool_name="understand_video",
        args_sha256="a" * 64,
        model_request_id=case.request_id,
        state="STARTED",
        metadata_json=encode({"kind": "qq_animation", "request_id": case.request_id}),
        created_at=core.clock(),
        updated_at=core.clock(),
    )
    before = core.store.one("SELECT * FROM agent_tool_calls")
    state = owner_input_state.read(core.store, core.settings, case.capture)
    assert state["unsettled_work"] is False and state["unsettled_model"] is True
    with core.store.transaction():
        view = owner_authority.custody(core.store, core.settings, settlement=True)
        value = model_carry.manifest(
            core.store,
            core.settings,
            case.active["authority"],
            case.request["successor_window_id"],
            case.verified.value,
            view,
        )
    case.request["manifest_sha256"] = digest(value)
    case.request["successor_policy"]["unknown_carry_sha256"] = digest(value)
    model_carry.prepare(core.store, core.settings, case.request, case.verified, core.clock())
    assert core.store.one("SELECT * FROM agent_tool_calls") == before


def test_fsync_before_projection_failure_requires_exact_controlled_recovery(
    core,
    tmp_path,
    monkeypatch,
):
    case = stranded(core, tmp_path)
    original = core.store.rows("SELECT * FROM model_usage")
    execute = core.store.execute

    def crash(query, values=()):
        if query == "INSERT INTO runtime_meta VALUES (?,?)" and values[0].startswith(
            "model_control:"
        ):
            raise RuntimeError("synthetic crash after fsync before projection")
        return execute(query, values)

    monkeypatch.setattr(core.store, "execute", crash)
    with pytest.raises(RuntimeError, match="after fsync"):
        model_carry.prepare(core.store, core.settings, case.request, case.verified, core.clock())
    monkeypatch.setattr(core.store, "execute", execute)
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        owner_authority.custody(core.store, core.settings, settlement=True)
    receipt = model_carry.prepare(
        core.store, core.settings, case.request, case.verified, core.clock()
    )
    assert receipt["carry_sha256"] == case.request["manifest_sha256"]
    assert core.store.rows("SELECT * FROM model_usage") == original
    records = [
        json.loads(row)
        for row in Path(core.settings.paid_model.journal_path)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert sum(row["kind"] == "model_unknown_carry" for row in records) == 1
    changed = case.request | {"authorization_sha256": "1" * 64}
    with pytest.raises(CoreError, match="OWNER_MODEL_CARRY_CHANGED"):
        model_carry.prepare(core.store, core.settings, changed, case.verified, core.clock())


def test_enrolled_cumulative_or_projection_is_an_explicit_blocker(core, tmp_path):
    from tests.support.cumulative_budget import enable

    enable(core, tmp_path)
    original = core.settings.paid_model
    core.settings.paid_model = original.model_copy(
        update={"concurrency_basis": "local_active_attempts_v2"}
    )
    with pytest.raises(CoreError, match="MODEL_CARRY_CUMULATIVE_UNSUPPORTED"):
        model_carry.supported(core.store, core.settings)
    core.settings.paid_model = original.model_copy(
        update={"concurrency_basis": "local_active_attempts_v2", "cumulative": None}
    )
    with pytest.raises(CoreError, match="MODEL_CARRY_CUMULATIVE_UNSUPPORTED"):
        model_carry.supported(core.store, core.settings)
    # No policy, pause or liability is rewritten in either case.
    assert (
        paid_budget.journal(core.store, original, allow_settlement=True)["cumulative"]["policy"]
        is not None
    )


@pytest.mark.parametrize("drift", ["journal", "verified-executors"])
def test_carry_rejects_changed_custody_before_appending(core, tmp_path, drift):
    case = stranded(core, tmp_path)
    path = Path(core.settings.paid_model.journal_path)
    before = path.read_bytes()
    if drift == "journal":
        core.settings.paid_model = core.settings.paid_model.model_copy(
            update={"journal_path": str(tmp_path / "different-journal.jsonl")}
        )
    else:
        case.request["executors"] = []
    with pytest.raises(CoreError):
        model_carry.prepare(core.store, core.settings, case.request, case.verified, core.clock())
    assert path.read_bytes() == before


def test_carry_journal_preserves_hashes_instead_of_private_policy(core, tmp_path):
    case = stranded(core, tmp_path)
    model_carry.prepare(core.store, core.settings, case.request, case.verified, core.clock())
    text = Path(core.settings.paid_model.journal_path).read_text(encoding="utf-8")
    assert '"owner_qq_id"' not in text
    assert '"successor_policy"' not in text
    record = json.loads(text.splitlines()[-1])
    assert record["successor_policy_sha256"] == digest(case.request["successor_policy"])


def test_legacy_defaults_do_not_change_frozen_approval_bytes(core, tmp_path):
    from tests.support.owner_authority_setup import prepare

    policy = prepare(core, tmp_path)
    assert "concurrency_basis" not in paid_budget.snapshot(core.settings)
    assert "unknown_carry_sha256" not in policy.model_dump(mode="json")


def test_local_close_projection_failure_keeps_future_admission_blocked(core, tmp_path, monkeypatch):
    import asyncio

    import httpx

    from companion import budget
    from companion.model import ModelClient
    from tests.model_carry_support import MESSAGES, migrated
    from tests.owner_model_fixtures import authorized_job

    migrated(core, tmp_path)
    job = authorized_job(core, number=2)
    request_id = budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock())
    original = core.store.one(
        "SELECT billed_micro_usd FROM model_usage WHERE request_id=?", (request_id,)
    )["billed_micro_usd"]
    execute = core.store.execute

    def fail(query, values=()):
        if query == "INSERT INTO runtime_meta VALUES (?,?)" and values[0].startswith(
            "model_control:"
        ):
            raise RuntimeError("synthetic local closure projection failure")
        return execute(query, values)

    async def exercise():
        def timeout(request):
            raise httpx.ReadTimeout("synthetic ended transport", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            with pytest.raises(RuntimeError, match="closure projection failure"):
                await budget.complete(
                    ModelClient(core.settings, client),
                    core.store,
                    core.settings,
                    request_id,
                    MESSAGES,
                    "chat",
                    core.clock,
                )

    monkeypatch.setattr(core.store, "execute", fail)
    asyncio.run(exercise())
    monkeypatch.setattr(core.store, "execute", execute)
    assert core.store.one(
        "SELECT billed_micro_usd,state FROM model_usage WHERE request_id=?", (request_id,)
    ) == {"billed_micro_usd": original, "state": "UNKNOWN"}
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        owner_authority.custody(core.store, core.settings, settlement=True)


def test_cancelled_transport_cleanup_precedes_local_closure(core, tmp_path):
    import asyncio

    import httpx

    from companion import budget
    from companion.model import ModelClient
    from tests.model_carry_support import MESSAGES, migrated
    from tests.owner_model_fixtures import authorized_job

    migrated(core, tmp_path)
    job = authorized_job(core, number=2)
    request_id = budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock())

    async def exercise():
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def transport(request):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaned.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            task = asyncio.create_task(
                budget.complete(
                    ModelClient(core.settings, client),
                    core.store,
                    core.settings,
                    request_id,
                    MESSAGES,
                    "chat",
                    core.clock,
                )
            )
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cleaned.is_set()
            with core.store.transaction():
                view = paid_budget.journal(core.store, core.settings.paid_model)
            assert request_id in view["model_closures"]

    asyncio.run(exercise())


def test_native_erasure_and_restore_preserve_carry_finance_without_reactivating(core, tmp_path):
    from cryptography.fernet import Fernet

    from companion.backup import backup, restore
    from companion.runtime import Runtime

    case = stranded(core, tmp_path)
    receipt = model_carry.prepare(
        core.store, core.settings, case.request, case.verified, core.clock()
    )
    before = core.store.rows("SELECT request_id,billed_micro_usd,state FROM model_usage")
    snapshot, restored, key = (
        tmp_path / "carry.enc",
        tmp_path / "carry-restored.db",
        Fernet.generate_key(),
    )
    backup(core.settings.database, snapshot, key)
    with core.store.transaction():
        core.ledger.expire_events(core.store, [case.capture.event_id], core.clock())
        view = paid_budget.journal(core.store, core.settings.paid_model, allow_settlement=True)
    assert receipt["carry_sha256"] in view["model_carries"]
    restore(snapshot, restored, key, core.settings.erasure_ledger)
    settings = core.settings.model_copy(update={"database": str(restored)})
    with Runtime(settings, core.clock) as runtime:
        assert owner_authority.status(runtime.store, settings, core.clock)["state"] == "OFF"
        assert (
            runtime.store.rows("SELECT request_id,billed_micro_usd,state FROM model_usage")
            == before
        )
        assert runtime.store.one(
            "SELECT redacted FROM events WHERE event_id=?", (case.capture.event_id,)
        )["redacted"]
        with pytest.raises(CoreError):
            model_carry.read(
                runtime.store,
                settings,
                case.active["authority"],
                receipt["carry_sha256"],
                receipt["successor_policy_sha256"],
            )

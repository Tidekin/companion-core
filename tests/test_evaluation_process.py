"""Actual TCP API and independent processes; provider transport remains mocked."""

import asyncio
from datetime import datetime

import httpx
import pytest
from pydantic import SecretStr

from companion import evaluation, owner_authority
from companion.store import after
from tests.support.core_http import real_core_http
from tests.support.evaluation import admitted_chat, authorize, prepare, result, work
from tests.support.evaluation_process import processes


@pytest.mark.parametrize("visual", [False, True], ids=["text", "image"])
def test_real_tcp_control_entry_and_two_independent_workers_share_one_job(core, tmp_path, visual):
    if visual:
        from tests.support.image_evaluation import prepared

        _, manifests = prepared(core, tmp_path)
        from scripts.evaluate_image_understanding import manifest as image_manifest

        manifest = evaluation.Manifest.model_validate(image_manifest(
            core.settings, manifests[0].plan["requests"][:6], False))
    else:
        manifest = prepare(core, tmp_path)
    core.settings.control_token = SecretStr("synthetic-control-" * 3)
    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + core.settings.control_token.get_secret_value()},
        ) as client:
            if visual:
                oversized = await client.post("/internal/v2/evaluations/preview",
                    content=b" " * (evaluation.MAX_HTTP_BODY_BYTES + 1))
                assert oversized.status_code == 413
                ordinary = await client.post("/internal/v1/inbox/events",
                                             json=manifest.model_dump())
                assert ordinary.status_code == 413
            preview = await client.post("/internal/v2/evaluations/preview",
                                        json=manifest.model_dump())
            assert preview.status_code == 200, preview.text
            granted = await client.post("/internal/v2/consents", json={
                **preview.json()["consent_scope"], "duration_seconds": 600})
            assert granted.status_code == 200
            submitted = await client.post("/internal/v2/evaluations", json={
                "manifest": manifest.model_dump(), "consent_id": granted.json()["consent_id"]})
            assert submitted.status_code == 202, submitted.text
            value = submitted.json()
            hashes = await processes(core, tmp_path / "parallel")
            assert hashes == [row["payload_sha256"] for row in manifest.plan["requests"]]
            observed = await client.get("/internal/v2/evaluations/" + value["evaluation_id"])
            assert observed.status_code == 200 and observed.headers["Cache-Control"] == "no-store"
            assert observed.json()["state"] == "SUCCEEDED"
            assert all(row["complete_response"] for row in observed.json()["requests"])
    asyncio.run(scenario())
    count = core.store.one("SELECT COUNT(*) n FROM model_usage")["n"]
    assert count == manifest.plan["request_count"]
    assert core.store.rows("SELECT * FROM outbox") == []
    assert core.store.rows("SELECT * FROM memories") == []


def test_process_death_after_dispatch_preserves_unknown_and_no_repayment(core, tmp_path):
    manifest = prepare(core, tmp_path)
    submitted = evaluation.submit(core, authorize(core, manifest))
    initial = asyncio.run(processes(core, tmp_path / "crashed", count=1, mode="crash"))
    assert len(initial) == 1
    assert core.store.one("SELECT state FROM model_usage")["state"] == "RESERVED"
    core.clock.advance(core.settings.lease_seconds + 1)
    restarted = asyncio.run(processes(core, tmp_path / "restarted", count=2))
    assert restarted == []
    assert core.store.one("SELECT state FROM model_usage")["state"] == "UNKNOWN"
    assert result(core, submitted)["state"] == "CANCELLED"
    assert core.store.rows("SELECT * FROM outbox") == []


def test_two_workers_account_for_chat_and_experiment_above_old_money_cap(core, tmp_path):
    manifest = prepare(core, tmp_path, chat=True)
    core.settings.cloud_budget = core.settings.cloud_budget.model_copy(
        update={})
    normal = admitted_chat(core)
    submitted = evaluation.submit(core, authorize(core, manifest))
    hashes = asyncio.run(processes(core, tmp_path / "competing", drain=True))
    assert len(hashes) == 7
    usage = core.store.rows("SELECT job_id,state,billed_micro_usd FROM model_usage")
    assert len(usage) == 7 and all(row["state"] == "SETTLED" for row in usage)
    eval_event = core.store.one('SELECT event_id FROM jobs WHERE job_id=%s', (submitted["job_id"],))
    assert not core.store.one('SELECT 1 FROM outbox WHERE origin_event_id=%s',
                              (eval_event["event_id"],))
    assert result(core, submitted)["state"] == "SUCCEEDED"
    normal_state = core.store.one('SELECT state FROM jobs WHERE job_id=%s', (normal["job_id"],))
    assert normal_state["state"] == "SUCCEEDED"


@pytest.mark.parametrize("boundary", ["unchanged", "stop", "paid_change", "expiry", "restore"])
def test_owner_authority_fences_old_experiment(core, tmp_path, boundary):
    from companion.owner_authority_protocol import AuthorityStop

    manifest = prepare(core, tmp_path)
    submitted = evaluation.submit(core, authorize(core, manifest))
    ref = owner_authority.current(core.store, core.settings, core.clock).authority.as_dict()
    if boundary == "stop":
        owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
            operation_id="operation-evaluation-stop", authority=ref))
    elif boundary == "paid_change":
        core.settings.paid_model = core.settings.paid_model.model_copy(
            update={"window_calls": core.settings.paid_model.window_calls + 1})
    elif boundary == "expiry":
        core.clock.now = core.settings.paid_model.expires_at
    elif boundary == "restore":
        owner_authority.restore(core.store)
    hashes = asyncio.run(processes(core, tmp_path / "authority-bound", count=2))
    assert len(hashes) == (6 if boundary == "unchanged" else 0)
    if boundary == "unchanged":
        observed = result(core, submitted)
        assert observed["state"] == "SUCCEEDED", observed["error_code"]
        assert core.store.one("SELECT COUNT(*) n FROM model_admissions")["n"] == 6
    assert core.store.rows("SELECT * FROM outbox") == []


def test_consent_expiry_during_final_budget_sync_never_dispatches(core, tmp_path, monkeypatch):
    from companion import cloud_budget

    manifest = prepare(core, tmp_path)
    request = authorize(core, manifest)
    evaluation.submit(core, request)
    original = cloud_budget.mark_dispatched
    def advance(store, kind, rid, now):
        original(store, kind, rid, now)
        core.clock.now = datetime.fromisoformat(after(now, 601))
    monkeypatch.setattr(cloud_budget, "mark_dispatched", advance)
    calls = []
    work(core, lambda req: calls.append(req))
    assert calls == []


def test_owner_stop_during_final_budget_sync_never_dispatches(core, tmp_path, monkeypatch):
    from companion import cloud_budget
    from companion.owner_authority_protocol import AuthorityStop

    manifest = prepare(core, tmp_path)
    evaluation.submit(core, authorize(core, manifest))
    ref = owner_authority.current(core.store, core.settings, core.clock).authority.as_dict()
    original = cloud_budget.mark_dispatched

    def stop(store, kind, rid, now):
        original(store, kind, rid, now)
        owner_authority.stop(store, core.settings, core.clock, AuthorityStop(
            operation_id="operation-final-dispatch-stop", authority=ref))

    monkeypatch.setattr(cloud_budget, "mark_dispatched", stop)
    calls = []
    work(core, lambda req: calls.append(req))
    assert calls == []


def test_explicit_revision_keeps_old_evaluation_cancelled_and_same_journal(core, tmp_path):
    manifest = prepare(core, tmp_path)
    core.settings.control_token = SecretStr("synthetic-control-" * 3)
    submitted = evaluation.submit(core, authorize(core, manifest))
    view = owner_authority.current(core.store, core.settings, core.clock)
    journal_id = view.journal_id

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + core.settings.control_token.get_secret_value()},
        ) as client:
            prefix = "/internal/v1/owner-authority"
            stopped = await client.post(prefix + "/stop", json={
                "operation_id": "operation-process-stop", "authority": view.authority.as_dict()})
            assert stopped.status_code == 200, stopped.text
            proposed = {"expected_revision": 2, "policy": view.policy.model_dump(mode="json")}
            preview = await client.post(prefix + "/preview", json=proposed)
            assert preview.status_code == 200, preview.text
            revised = await client.post(prefix + "/revisions", json={**proposed,
                "operation_id": "operation-process-revision",
                "preview_sha256": preview.json()["preview_sha256"]})
            assert revised.status_code == 200, revised.text
            assert await processes(core, tmp_path / "after-revision") == []
    asyncio.run(scenario())
    assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (submitted["job_id"],))[
        "state"] == "CANCELLED"
    assert core.store.one("SELECT value FROM runtime_meta "
                          "WHERE key='model_budget_journal_id'")["value"] == journal_id
    assert core.store.rows("SELECT * FROM model_usage") == []
    assert core.store.rows("SELECT * FROM outbox") == []

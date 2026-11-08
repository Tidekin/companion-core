import json
import os
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from companion import budget
from companion.contracts import CoreError
from companion.dsh_job import run_job
from companion.executive import Executive, HTTPExecutor, erase_executive
from companion.executive_protocol import (
    ExecutorEndpoint,
    ToolArguments,
    ToolDecision,
    ToolReceipt,
)
from companion.executor_budget_proxy import create_budget_proxy
from companion.executor_gateway import FileExecutor, create_executor_app, receipt
from companion.postgres import Database
from companion.privacy import advance_epoch
from tests.conftest import run
from tests.support.executive import TOKEN, FakeExecutor, configured, envelope, proposal


def test_tools_are_disabled_by_default_and_l3_cannot_be_requested(core):
    executive = Executive(core.store, core.settings, core.clock)
    with pytest.raises(CoreError, match="DISABLED"):
        executive.submit(proposal())
    with pytest.raises(ValidationError):
        proposal(capability="windows.shell")
    executive = configured(core)
    with pytest.raises(CoreError, match="CHANGES_DISABLED"):
        executive.submit(proposal(capability="windows.write_text", text="content"))


def test_readonly_idempotency_real_receipt_observation_and_ancestry(core):
    fake = FakeExecutor()
    executive = configured(core, fake)
    task = executive.submit(proposal())
    assert task["state"] == "QUEUED"
    assert executive.submit(proposal())["task_id"] == task["task_id"]
    with pytest.raises(CoreError, match="IDEMPOTENCY_CONFLICT"):
        executive.submit(
            proposal().model_copy(update={"arguments": ToolArguments(path="other.txt")})
        )
    assert run(executive.run_one())
    result = executive.get(task["task_id"])
    assert result["state"] == "SUCCEEDED"
    observation = core.store.one(
        'SELECT * FROM observations WHERE event_id=%s', (result["result_event_id"],)
    )
    assert observation["source_kind"] == "tool_result"
    assert observation["result_status"] == "FETCHED"
    assert executive.sources(task["task_id"]) == ["evt_test_1"]
    assert (
        core.store.one(
            'SELECT * FROM event_dependencies WHERE event_id=%s', (result["result_event_id"],)
        )["source_event_id"]
        == "evt_test_1"
    )
    assert not run(executive.run_one()) and len(fake.calls) == 1


def test_changes_need_separate_exact_immutable_approval(core):
    fake = FakeExecutor()
    executive = configured(core, fake, allow_changes=True)
    task = executive.submit(proposal(capability="windows.write_text", text="approved content"))
    assert task["state"] == "WAITING_APPROVAL" and not executive.claim()
    with pytest.raises(CoreError, match="SCOPE_CHANGED"):
        executive.decide(task["task_id"], ToolDecision(decision="APPROVE", scope_sha256="0" * 64))
    executive.decide(
        task["task_id"], ToolDecision(decision="APPROVE", scope_sha256=task["scope_sha256"])
    )
    assert run(executive.run_one())
    assert executive.get(task["task_id"])["state"] == "SUCCEEDED"
    assert len(fake.calls) == 1


def test_approval_expiry_and_capability_disabling_block_dispatch(core):
    fake = FakeExecutor()
    executive = configured(core, fake, allow_changes=True)
    task = executive.submit(proposal(capability="windows.write_text", text="approved"))
    executive.decide(
        task["task_id"], ToolDecision(decision="APPROVE", scope_sha256=task["scope_sha256"])
    )
    core.clock.advance(3601)
    assert run(executive.run_one())
    assert executive.get(task["task_id"])["state"] == "CANCELLED"
    assert not fake.calls


def test_approved_target_cannot_move_to_a_different_executor(core):
    fake = FakeExecutor()
    executive = configured(core, fake, allow_changes=True)
    task = executive.submit(proposal(capability="windows.write_text", text="approved"))
    executive.decide(
        task["task_id"], ToolDecision(decision="APPROVE", scope_sha256=task["scope_sha256"])
    )
    executive.config.endpoints["windows"].url = "http://127.0.0.1:8992"
    assert run(executive.run_one())
    assert executive.get(task["task_id"])["last_error_code"] == "TOOL_CAPABILITY_UNAVAILABLE"
    assert not fake.calls


def test_revocation_during_execution_never_accepts_late_result(core):
    fake = FakeExecutor()
    executive = configured(core, fake)
    task = executive.submit(proposal())
    fake.callback = lambda: executive.revoke(task["task_id"])
    assert run(executive.run_one())
    assert executive.get(task["task_id"])["state"] == "UNKNOWN"
    assert core.store.rows("SELECT * FROM observations") == []


def test_monthly_money_limit_is_advisory_and_each_unknown_tool_charge_remains(core):
    fake = FakeExecutor()
    fake.fail = True
    core.clock.now = datetime.fromisoformat("2026-09-30T16:00:00+00:00")
    executive = configured(core, fake)
    executive.submit(proposal())
    assert run(executive.run_one())
    core.clock.advance(10 * 86400)
    second = executive.submit(proposal(2))
    assert run(executive.run_one())
    assert executive.get(second["task_id"])["last_error_code"] == "EXECUTION_UNCERTAIN"
    assert len(fake.calls) == 2
    assert core.store.one("SELECT sum(billed_micro_usd) AS n FROM tool_tasks")["n"] == 0


def test_tool_runs_beyond_advisory_shared_monthly_amount(core):
    fake = FakeExecutor()
    executive = configured(core, fake)
    job = core.store.one("SELECT job_id FROM jobs")["job_id"]
    budget.reserve(
        core.store, core.settings, job, "chat", [{"role": "user", "content": "hello"}], core.clock()
    )
    task = executive.submit(proposal())
    assert run(executive.run_one())
    assert executive.get(task["task_id"])["state"] == "SUCCEEDED"
    assert len(fake.calls) == 1


def test_unknown_does_not_retry_and_retains_budget(core):
    fake = FakeExecutor()
    fake.fail = True
    executive = configured(core, fake)
    task = executive.submit(proposal())
    assert run(executive.run_one())
    row = executive.get(task["task_id"])
    assert row["state"] == "UNKNOWN" and row["billed_micro_usd"] == 0
    assert not row["result_event_id"] and not run(executive.run_one())
    assert len(fake.calls) == 1


def test_call_limits_reserve_before_dispatch(core):
    fake = FakeExecutor()
    executive = configured(core, fake)
    executive.config.daily_calls = 1
    executive.submit(proposal())
    second = executive.submit(proposal(2))
    assert run(executive.run_one()) and run(executive.run_one())
    assert len(fake.calls) == 1
    assert executive.get(second["task_id"])["last_error_code"] == "TOOL_CALL_LIMIT"


def test_stale_lease_cannot_commit_or_redispatch_started_effect(core):
    executive = configured(core)
    original = executive.submit(proposal())
    lease = executive.claim()
    _, request = executive.begin(lease)
    core.clock.advance(100)
    executive.recover()
    assert executive.get(original["task_id"])["state"] == "UNKNOWN"
    with pytest.raises(CoreError, match="STALE_TOOL_LEASE"):
        executive.finish(lease, receipt(request, "SUCCEEDED", "late result"))
    assert not executive.claim()


def test_pre_dispatch_lease_can_recover_without_counting_call(core):
    executive = configured(core)
    executive.submit(proposal())
    first = executive.claim()
    core.clock.advance(100)
    second = executive.claim()
    assert first["lease_token"] != second["lease_token"]
    with pytest.raises(CoreError, match="STALE_TOOL_LEASE"):
        executive.begin(first)
    assert not second["dispatched_at"]


def test_privacy_epoch_change_during_network_call_suppresses_projection(core):
    fake = FakeExecutor()
    executive = configured(core, fake)
    task = executive.submit(proposal())
    fake.callback = lambda: advance_epoch(core.store)
    assert run(executive.run_one())
    assert executive.get(task["task_id"])["state"] == "UNKNOWN"
    assert core.store.rows("SELECT * FROM observations") == []


def test_erasure_hook_redacts_scope_approval_and_evidence(core):
    executive = configured(core, allow_changes=True)
    task = executive.submit(proposal(capability="windows.write_text", text="private draft"))
    executive.decide(
        task["task_id"], ToolDecision(decision="APPROVE", scope_sha256=task["scope_sha256"])
    )
    with core.store.transaction():
        erase_executive(core.store, ["evt_test_1"])
    result = executive.get(task["task_id"])
    assert result["state"] == "CANCELLED" and result["scope_json"] == "{}"
    assert core.store.rows("SELECT * FROM tool_approvals") == []
    assert not executive.sources(task["task_id"])
    with pytest.raises(CoreError, match="SOURCE_ERASED"):
        executive.submit(proposal(capability="windows.write_text", text="private draft"))


def test_restore_revokes_queued_approval_and_running_becomes_unknown(
    empty_database, core, tmp_path,
):
    from cryptography.fernet import Fernet

    from companion.backup import backup, restore
    from companion.store import Store

    executive = configured(core, allow_changes=True)
    first = executive.submit(proposal())
    executive.begin(executive.claim())
    second = executive.submit(proposal(2, "windows.write_text", text="approved"))
    executive.decide(
        second["task_id"], ToolDecision(decision="APPROVE", scope_sha256=second["scope_sha256"])
    )
    key, archive, restored_state = (
        Fernet.generate_key(),
        tmp_path / "tools.enc",
        tmp_path / "restored-state",
    )
    backup(core.settings.database, archive, key)
    restore(archive, empty_database, key, core.settings.erasure_ledger,
            state_directory=restored_state)
    with (
        Database(empty_database) as database,
        closing(Store(database, restored_state)) as restored,
    ):
        assert (
            restored.one("SELECT state FROM tool_tasks WHERE task_id=%s", (first["task_id"],))[
                "state"
            ]
            == "UNKNOWN"
        )
        assert (
            restored.one("SELECT state FROM tool_tasks WHERE task_id=%s", (second["task_id"],))[
                "state"
            ]
            == "CANCELLED"
        )
        assert restored.one("SELECT revoked_at FROM tool_approvals")["revoked_at"]


def test_receipt_digest_and_task_identity_are_checked(core):
    with pytest.raises(ValidationError):
        ToolReceipt(
            task_id="fake",
            scope_sha256="a" * 64,
            status="SUCCEEDED",
            text="forged",
            evidence_sha256="0" * 64,
            billed_micro_usd=0,
        )
    executive = configured(core)
    executive.submit(proposal())
    lease = executive.claim()
    _, request = executive.begin(lease)
    forged = receipt(request, "SUCCEEDED", "content").model_copy(update={"task_id": "wrong"})
    with pytest.raises(CoreError, match="INVALID_TOOL_RECEIPT"):
        executive.finish(lease, forged)


def test_http_executor_does_not_follow_redirect_or_repeat_requests():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"location": "https://foreign.invalid/exfil"})

    endpoint = ExecutorEndpoint(
        url="http://127.0.0.1:8991",
        token=TOKEN,
        workspace="work",
        capabilities=["windows.read_text"],
    )
    with pytest.raises(httpx.HTTPStatusError):
        run(HTTPExecutor(httpx.MockTransport(handler)).execute(endpoint, envelope()))
    assert len(calls) == 1
    assert calls[0].headers["authorization"] == "Bearer " + TOKEN


def test_gateway_reads_real_file_and_never_replays_write(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "result.txt").write_text("real fixture content", encoding="utf-8")
    app = create_executor_app(
        TOKEN, tmp_path / "gateway.db", FileExecutor({"work": work}, allow_changes=True)
    )
    try:
        with TestClient(app) as client:
            request = envelope().model_dump()
            assert client.post("/internal/v1/execute", json=request).status_code == 401
            auth = {"Authorization": "Bearer " + TOKEN}
            response = client.post("/internal/v1/execute", headers=auth, json=request).json()
            assert response["status"] == "SUCCEEDED" and response["text"] == "real fixture content"
            again = client.post("/internal/v1/execute", headers=auth, json=request).json()
            assert again["status"] == "UNKNOWN"
            write = envelope(
                task_id="tool_write",
                capability="windows.write_text",
                arguments={"path": "new.txt", "text": "exact approved content"},
            )
            assert (
                client.post("/internal/v1/execute", headers=auth, json=write.model_dump()).json()[
                    "status"
                ]
                == "SUCCEEDED"
            )
            assert (work / "new.txt").read_text("utf-8") == "exact approved content"
            assert (
                client.post("/internal/v1/execute", headers=auth, json=write.model_dump()).json()[
                    "status"
                ]
                == "UNKNOWN"
            )
    finally:
        app.state.db.close()


def test_core_approval_http_gateway_real_file_and_observation(core, tmp_path):
    work = tmp_path / "deliverables"
    work.mkdir()
    gateway = create_executor_app(
        TOKEN, tmp_path / "executor.db", FileExecutor({"work": work}, allow_changes=True)
    )
    try:
        adapter = HTTPExecutor(httpx.ASGITransport(app=gateway))
        executive = configured(core, adapter, allow_changes=True)
        task = executive.submit(proposal(capability="windows.write_text", text="exact bytes 测试"))
        assert not run(executive.run_one()) and not (work / "result.txt").exists()
        executive.decide(
            task["task_id"], ToolDecision(decision="APPROVE", scope_sha256=task["scope_sha256"])
        )
        assert run(executive.run_one())
        assert (work / "result.txt").read_bytes() == "exact bytes 测试".encode()
        read = executive.submit(proposal(2))
        assert run(executive.run_one())
        result = executive.get(read["task_id"])
        event = core.store.one(
            'SELECT body_json FROM events WHERE event_id=%s', (result["result_event_id"],)
        )
        assert json.loads(event["body_json"])["text"] == "exact bytes 测试"
    finally:
        gateway.state.db.close()


@pytest.mark.parametrize(
    "path",
    [
        "../secret.txt",
        "C:/secret.txt",
        "/secret.txt",
        ".env",
        "db.sqlite3",
        "CON.txt",
        "NUL.txt",
        "x.txt ",
        "dir./x.txt",
    ],
)


def test_gateway_rejects_outside_scope_paths(tmp_path, path):
    backend = FileExecutor({"work": tmp_path})
    result = run(backend.execute(envelope(arguments={"path": path})))
    assert result.status == "FAILED"


def test_gateway_refuses_hardlinked_file(tmp_path):
    outside = tmp_path / "private.txt"
    outside.write_text("private", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    os.link(outside, work / "result.txt")
    assert run(FileExecutor({"work": work}).execute(envelope())).status == "FAILED"


def test_gateway_mcp_exposes_only_scoped_executor(tmp_path):
    app = create_executor_app(TOKEN, tmp_path / "gateway.db")
    try:
        with TestClient(app) as client:
            answer = client.post(
                "/mcp",
                headers={"Authorization": "Bearer " + TOKEN},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            ).json()
            assert [tool["name"] for tool in answer["result"]["tools"]] == ["execute_scoped"]
    finally:
        app.state.db.close()


def test_proxy_keeps_call_token_and_revocation_controls_with_advisory_money():
    upstream_calls = []

    def upstream(request):
        upstream_calls.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer private-upstream-key"
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

    app = create_budget_proxy(
        TOKEN,
        "https://model.example/v1",
        "private-upstream-key",
        "fixed-model",
        300000,
        1200000,
        transport=httpx.MockTransport(upstream),
    )
    auth = {"Authorization": "Bearer " + TOKEN}
    with TestClient(app) as client:
        grant = client.post(
            "/internal/v1/reservations", headers=auth, json=envelope().model_dump()
        ).json()
        scoped_auth = {"Authorization": "Bearer " + grant["token"]}
        body = {
            "model": "attacker-model",
            "max_tokens": 999999,
            "n": 1000,
            "messages": [{"role": "user", "content": "hello"}],
        }
        assert (
            client.post("/v1/chat/completions", headers=scoped_auth, json=body).status_code == 200
        )
        assert upstream_calls[0]["model"] == "fixed-model"
        assert upstream_calls[0]["max_tokens"] == 1024
        assert upstream_calls[0]["n"] == 1
        assert (
            client.post("/v1/chat/completions", headers=scoped_auth, json=body).status_code == 429
        )
        usage = client.get("/internal/v1/reservations/" + grant["token"], headers=auth).json()
        assert usage == {"calls": 1, "billed_micro_usd": 0, "input_tokens": 0,
                         "output_tokens": 0, "unknown_calls": 1}
        client.delete("/internal/v1/reservations/" + grant["token"], headers=auth)
        assert (
            client.post("/v1/chat/completions", headers=scoped_auth, json=body).status_code == 403
        )
        small = envelope().model_dump()
        reduced = client.post("/internal/v1/reservations", headers=auth, json=small)
        assert reduced.status_code == 200
        small_usage = client.get("/internal/v1/reservations/" + reduced.json()["token"],
                                 headers=auth).json()
        assert small_usage["calls"] == small_usage["billed_micro_usd"] == 0
        assert small_usage["unknown_calls"] == 0
    assert len(upstream_calls) == 1


def test_executor_compose_separates_model_secret_network_and_core_storage():
    import yaml

    compose = yaml.safe_load(
        (Path(__file__).parents[1] / "deploy/compose.executors.yaml").read_text("utf-8")
    )
    executor, proxy = (compose["services"][name] for name in ("tool-executor", "budget-proxy"))
    assert list(executor["networks"]) == ["tool-private"]
    assert compose["networks"]["tool-private"]["internal"] is True
    assert executor["volumes"] == ["executor-state:/state"]
    assert executor["read_only"] and executor["cap_drop"] == ["ALL"]
    assert "EXECUTOR_MODEL_KEY" not in executor["environment"]
    assert "EXECUTOR_MODEL_KEY" in proxy["environment"]
    assert "pid" not in executor and "privileged" not in executor
    assert executor["environment"]["EXECUTOR_DSH_ENABLED"].endswith(":-false}")
    assert proxy["tmpfs"] == ["/tmp:rw,nosuid,nodev,size=16m"]


def test_sdk_adapter_requires_terminal_and_real_text_not_failed_tool_claim(tmp_path):
    class Harness:
        result = SimpleNamespace(
            finish_reason="completed", events=[], final_response="actual result"
        )

        def __init__(self, **config):
            assert config["profile"] == "sdk-minimal"
            assert config["api_key"] == "scoped-ephemeral-token"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, prompt):
            return self.result

    body = dict(
        home=str(tmp_path / "home"),
        workspace=str(tmp_path / "work"),
        model="fixed-model",
        max_tokens=1024,
        proxy_url="http://proxy/v1",
        token="scoped-ephemeral-token",
        timeout=20,
        prompt="calculate",
    )
    assert run_job(body, Harness)["status"] == "SUCCEEDED"
    Harness.result.events = [{"type": "tool/result", "data": {"isError": True}}]
    body.update(home=str(tmp_path / "home2"), workspace=str(tmp_path / "work2"))
    assert run_job(body, Harness)["status"] == "UNKNOWN"


def test_core_tool_routes_require_owner_control_and_exact_scope(core):
    from pydantic import SecretStr

    from companion.api import create_app

    configured(core, allow_changes=True)
    core.settings.control_token = SecretStr("owner-control-token-" * 3)
    bridge = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
    owner = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
    with TestClient(create_app(core.settings)) as client:
        body = proposal(capability="windows.write_text", text="approved content").model_dump()
        assert client.post("/internal/v2/tools", json=body, headers=bridge).status_code == 401
        response = client.post("/internal/v2/tools", json=body, headers=owner)
        assert response.status_code == 202
        task = response.json()
        assert task["state"] == "WAITING_APPROVAL"
        path = "/internal/v2/tools/" + task["task_id"] + "/decision"
        assert (
            client.post(
                path, json={"decision": "APPROVE", "scope_sha256": "0" * 64}, headers=owner
            ).status_code
            == 409
        )
        assert (
            client.post(
                path,
                json={"decision": "APPROVE", "scope_sha256": task["scope_sha256"]},
                headers=owner,
            ).json()["state"]
            == "QUEUED"
        )
        assert client.post("/internal/v1/tasks", json=body, headers=bridge).status_code == 403


def test_unknown_tool_cost_remains_while_fresh_model_reservation_continues(core):
    adapter = FakeExecutor()
    adapter.fail = True
    executive = configured(core, adapter)
    executive.submit(proposal())
    assert run(executive.run_one())
    assert core.store.one("SELECT state FROM tool_tasks")["state"] == "UNKNOWN"
    before = core.store.one("SELECT state,billed_micro_usd FROM tool_tasks")
    budget.reserve(core.store, core.settings, None, "chat",
                   [{"role": "user", "content": "test"}], core.clock())
    assert core.store.one("SELECT state,billed_micro_usd FROM tool_tasks") == before
    assert core.store.one("SELECT state FROM model_usage")["state"] == "RESERVED"


def test_settings_reject_reusing_executor_and_bridge_tokens(core):
    from companion.config import Settings

    configured(core)
    core.settings.executive.endpoints["windows"].token = core.settings.bridge_token
    with pytest.raises(ValidationError, match="executor tokens must be separate"):
        Settings.model_validate(core.settings.model_dump())


@pytest.mark.parametrize("create", [False, True])
def test_directory_replacement_cannot_redirect_scoped_file_access(tmp_path, monkeypatch, create):
    import companion.executor_gateway as gateway

    root, other = tmp_path / "work", tmp_path / "other"
    nested, prepared = root / "nested", tmp_path / "replacement"
    nested.mkdir(parents=True)
    other.mkdir()
    if not create:
        (nested / "result.txt").write_text("inside", encoding="utf-8")
        (other / "result.txt").write_text("independent file", encoding="utf-8")
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(other), str(prepared))
    else:
        prepared.symlink_to(other, target_is_directory=True)
    original = gateway.scoped_path

    def replace_directory(base, relative):
        path = original(base, relative)
        nested.rename(root / "original")
        prepared.rename(nested)
        return path

    monkeypatch.setattr(gateway, "scoped_path", replace_directory)
    request = envelope(
        capability="windows.write_text" if create else "windows.read_text",
        arguments={"path": "nested/result.txt", "text": "approved" if create else ""},
    )
    result = run(FileExecutor({"work": root}, allow_changes=True).execute(request))
    assert result.status == "FAILED"
    if create:
        assert not (other / "result.txt").exists()
    else:
        assert (other / "result.txt").read_text("utf-8") == "independent file"


def test_open_file_retains_original_object_and_windows_holds_parent(tmp_path):
    from companion.executor_files import open_scoped

    root = tmp_path / "held-directory"
    root.mkdir()
    path = root / "result.txt"
    path.write_text("original", encoding="utf-8")
    with open_scoped(path) as stream:
        if os.name == "nt":
            with pytest.raises(OSError):
                root.rename(tmp_path / "renamed")
            with pytest.raises(OSError):
                path.write_text("changed", encoding="utf-8")
        else:
            path.rename(root / "original.txt")
            path.write_text("different object", encoding="utf-8")
        assert stream.read() == b"original"
    root.rename(tmp_path / "released-directory")


def test_executive_worker_has_dedicated_single_instance_lock(tmp_path):
    from companion.worker_lock import single_worker

    state_directory = tmp_path / "state"
    with single_worker(state_directory, "executive-worker"), single_worker(state_directory):
        with pytest.raises(CoreError, match="WORKER_ALREADY_RUNNING"):
            with single_worker(state_directory, "executive-worker"):
                pass

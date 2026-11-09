"""Definite pre-effect refusals replay their error; ambiguous native effects stay fenced."""

import json
import subprocess
import sys

import pytest

from companion.contracts import CoreError
from companion.diary import DiaryService
from companion.goals import GoalEngine
from companion.memory import Memory
from companion.store import encode
from tests.support import owner_console as test_owner_console


@pytest.fixture
def client(core, monkeypatch):
    yield from test_owner_console.client.__wrapped__(core, monkeypatch)


@pytest.mark.parametrize("case,code,status", [
    ("missing", "MEMORY_NOT_FOUND", 404),
    ("inactive", "MEMORY_NOT_ACTIVE", 409),
    ("blank", "CORRECTION_REQUIRED", 400),
    ("credential", "CREDENTIAL_MEMORY_DENIED", 400),
    ("diary_invalid", "INVALID_DIARY_CORRECTION", 400),
    ("diary_disabled", "DIARY_DISABLED_IN_CONFIG", 409),
    ("proactive_disabled", "PROACTIVE_DISABLED_IN_CONFIG", 409),
])
def test_native_pre_effect_rejections_replay_without_reexecuting(
    client, core, monkeypatch, case, code, status,
):
    memory = "mem_missing"
    if case in {"inactive", "blank", "credential"}:
        memory, _ = test_owner_console.stored_memory(core)
    if case == "inactive":
        core.store.execute("UPDATE memories SET status='SUPERSEDED' WHERE memory_id=%s", (memory,))
    route = f"/v1/console/memories/{memory}/decision"
    body = {"decision": "confirm"}
    if case in {"blank", "credential"}:
        body = {"decision": "correct", "corrected_content": (
            " " if case == "blank" else test_owner_console.PASSWORD)}
    if case == "diary_invalid":
        route, body = "/v1/console/diary/2026-10-19/correction", {"text": " "}
    if case.endswith("disabled"):
        route = "/v1/console/mode"
        body = {"target": case.split("_")[0], "enabled": True}
    headers = test_owner_console.login(client)
    tables = ("events", "memories", "memory_revisions", "diary_entries", "diary_revisions",
              "jobs", "runtime_meta", "privacy_erasure_ledger")
    before = {name: core.store.rows("SELECT * FROM " + name) for name in tables}
    first = client.post(route, json=body, headers=headers)
    assert first.status_code == status and first.json() == {"error": code}
    assert {name: core.store.rows("SELECT * FROM " + name) for name in tables} == before
    row = core.store.one("SELECT * FROM console_operations")
    assert row["state"] == "REJECTED"
    assert json.loads(row["result_json"]) == {"error": code, "status": status}
    assert test_owner_console.PASSWORD not in encode(core.store.rows("SELECT * FROM audit_log"))
    # Even if the original precondition later changes, the same request must only
    # replay its terminal rejection; it may not execute a fresh native operation.
    def forbidden(*args, **kwargs):
        raise AssertionError("terminal rejection must not reexecute")
    monkeypatch.setattr(Memory, "decide", forbidden)
    monkeypatch.setattr(DiaryService, "correct", forbidden)
    monkeypatch.setattr(DiaryService, "set_mode", forbidden)
    core.settings.proactive_enabled = True
    repeated = client.post(route, json=body, headers=headers)
    assert repeated.status_code == status and repeated.json() == first.json()
    altered = {**body, "decision": "forget"} if route.endswith("/decision") else body
    if altered != body:
        assert client.post(route, json=altered, headers=headers).json() == {
            "error": "IDEMPOTENCY_CONFLICT"}


def test_unknown_native_error_keeps_started_and_does_not_reexecute(client, core, monkeypatch):
    calls = []
    def uncertain(*args, **kwargs):
        calls.append(1)
        raise CoreError("UNRECOGNIZED_NATIVE_FAILURE", 503)
    monkeypatch.setattr(GoalEngine, "pause_autonomy", uncertain)
    headers = test_owner_console.login(client)
    body = {"target": "life", "enabled": False}
    assert client.post("/v1/console/mode", json=body, headers=headers).status_code == 503
    assert core.store.one("SELECT state FROM console_operations")["state"] == "STARTED"
    repeated = client.post("/v1/console/mode", json=body, headers=headers)
    assert repeated.status_code == 409
    assert repeated.json()["error"] == "CONSOLE_OPERATION_RECONCILIATION_REQUIRED"
    assert calls == [1]


def test_process_death_after_native_commit_stays_started_on_authenticated_retry(client, core):
    headers = test_owner_console.login(client)
    script = '''
import os,sys
from types import SimpleNamespace
from companion.config import Settings
from companion.core import Core
from companion.console_api import _operation
settings=Settings(owner_qq_id="10001",bot_qq_id="10002",bridge_token="synthetic-token-"*3,
                  database=sys.argv[1],erasure_ledger=sys.argv[2],state_directory=sys.argv[5])
with Core(settings,clock=lambda:sys.argv[3]) as core:
    def apply():
        core.goals.pause_autonomy(True)
        os._exit(23)
    request=SimpleNamespace(method="POST",url=SimpleNamespace(path="/v1/console/mode"))
    _operation(core,request,sys.argv[4],{"target":"life","enabled":False},apply)
'''
    child = subprocess.run([sys.executable, "-B", "-c", script,
                            core.settings.database.get_secret_value(),
                            core.settings.erasure_ledger, core.clock(),
                            headers["Idempotency-Key"], core.settings.state_directory],
                           capture_output=True, timeout=20)
    assert child.returncode == 23
    assert core.store.one("SELECT value FROM runtime_meta WHERE key='life_paused'")["value"] == "1"
    assert core.store.one("SELECT state FROM console_operations")["state"] == "STARTED"
    # A newer independent owner action must not be overwritten by replaying the crash gap.
    core.goals.pause_autonomy(False)
    repeated = client.post("/v1/console/mode", json={"target": "life", "enabled": False},
                           headers=headers)
    assert repeated.status_code == 409
    assert core.store.one("SELECT value FROM runtime_meta WHERE key='life_paused'")["value"] == "0"

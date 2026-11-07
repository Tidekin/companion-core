"""Optional capability jobs run through the same bounded native selector."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from companion import owner_authority, worker_deadlines
from companion.autonomous_selector import native_selector
from companion.poke_protocol import PokePolicy
from tests.conftest import run
from tests.meme_authority_fixture import environment
from tests.meme_authority_fixture import stop as stop_meme
from tests.poke_authority_fixture import environment as poke_environment
from tests.poke_authority_fixture import owner_turn, signed
from tests.support.core_http import real_core_http
from tests.support.meme_core import body
from tests.support.meme_owner_authority import configure, discovery
from tests.support.meme_service import result
from tests.support.owner_authority import activate
from tests.support.selector_capabilities import child


@pytest.mark.parametrize("memory_enabled", [False, True])
def test_m0_m1_never_invoke_live_capability_operations(core, monkeypatch, memory_enabled):
    core.settings.memory_enabled = memory_enabled
    core.settings.poke_policy = PokePolicy(enabled=True)
    core.settings.meme_semantics_enabled = True

    async def forbidden(*args):
        pytest.fail("mock M0/M1 must not invoke an external capability worker")

    monkeypatch.setattr(core, "_run_poke_once", forbidden)
    monkeypatch.setattr(core.memes, "run_once", forbidden)
    lanes = native_selector(core, "closed-capabilities").lanes
    for lane in lanes:
        if lane.name in {"poke", "meme_caption"}:
            assert not run(lane.run()).progressed
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_actual_caption_job_runs_in_independent_selector_without_core_fallback(
    core, tmp_path, monkeypatch,
):
    paid = configure(core, caption=True)

    async def scenario():
        async with environment(core, tmp_path, monkeypatch, paid=paid) as values:
            client, _, grant, now = values
            event, item = await discovery(core, *values)
            result(core, item)
            created = await client.post("/internal/v1/memes/captions",
                                        json=body(core, event, item, now))
            assert created.status_code == 200, created.text
            job_id = created.json()["job_id"]
            first = await child(core, tmp_path, "meme_caption", 1)
            assert len(first["calls"]) == 1, first
            assert {"lane": "meme_caption", "progressed": True} in first["turns"]
            job = core.store.one('SELECT state,authority_json FROM jobs WHERE job_id=%s', (job_id,))
            assert job["state"] == "SUCCEEDED"
            assert json.loads(job["authority_json"]) == grant.authority
            usage = core.store.rows("SELECT * FROM model_usage")
            assert len(usage) == 1 and usage[0]["scenario"] == "meme_caption"
            assert usage[0]["state"] == "SETTLED"
            second = await child(core, tmp_path, "meme_caption", 2)
            assert second["calls"] == []
            assert core.store.rows("SELECT * FROM model_usage") == usage
            assert core.store.rows("SELECT * FROM outbox") == []
    run(scenario())


@pytest.mark.parametrize("fence", ["purpose", "pre_ingress"])
def test_ready_native_caption_remains_fenced_before_selector_dispatch(
    core, tmp_path, monkeypatch, fence,
):
    paid = configure(core, caption=True)

    async def scenario():
        async with environment(core, tmp_path, monkeypatch, paid=paid) as values:
            client, config, grant, now = values
            event, item = await discovery(core, *values)
            result(core, item)
            created = await client.post("/internal/v1/memes/captions",
                                        json=body(core, event, item, now))
            assert created.status_code == 200, created.text
            job_id = created.json()["job_id"]
            original = core.store.one('SELECT authority_json FROM jobs WHERE job_id=%s', (job_id,))
            if fence == "purpose":
                policy = grant.policy.model_copy(update={
                    "purposes": [p for p in grant.policy.purposes if p != "meme"],
                    "capability_policies": {},
                })
                stopped = await stop_meme(client, grant)
                assert stopped.status_code == 200, stopped.text
                activate(core, policy, revision=stopped.json()["authority"]["revision"],
                         operation="caption-purpose-withdrawn")
            else:
                view = owner_authority.current(core.store, core.settings, core.clock)
                capture = owner_turn(SimpleNamespace(core=core, store=core.store,
                    settings=core.settings, clock=core.clock, approval=view,
                    key=grant.key, config=config), 2, "先处理新输入", approve_only=True)
                assert not core.store.one('SELECT 1 FROM events WHERE event_id=%s',
                                           (capture["event_id"],))
            report = await child(core, tmp_path, "meme_caption", 1)
            assert report["calls"] == []
            assert core.store.rows("SELECT * FROM model_usage") == []
            assert core.store.one('SELECT authority_json FROM jobs WHERE job_id=%s',
                                   (job_id,)) == original
            if fence == "pre_ingress":
                assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (job_id,)) == {
                    "state": "QUEUED",
                }
    run(scenario())


@pytest.mark.parametrize("pre_ingress", [False, True])
def test_native_poke_coalescing_and_execution_use_original_selector_process(
    core, tmp_path, pre_ingress,
):
    env = poke_environment(core, tmp_path)
    capture = signed(env)

    async def scenario():
        async with real_core_http(core) as port:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                headers={"Authorization": "Bearer "
                         + core.settings.bridge_token.get_secret_value()}) as client:
                admitted = await client.post("/internal/v1/pokes",
                                              json=capture.model_dump(mode="json"))
                assert admitted.status_code == 200, admitted.text
                original = core.store.one(
                    "SELECT job_id,authority_json FROM jobs WHERE kind='poke'")
                start = datetime.fromisoformat(core.clock())
                deadline = worker_deadlines.life(core.store, core.settings, start)
                assert start < deadline <= start + timedelta(
                    seconds=core.settings.poke_policy.coalesce_seconds + 1)
                early = await child(core, tmp_path, "poke", 1)
                assert early["calls"] == []
                assert core.store.one("SELECT state FROM jobs WHERE kind='poke'") == {
                    "state": "QUEUED"}
                core.clock.advance(core.settings.poke_policy.coalesce_seconds + 1)
                if pre_ingress:
                    event = owner_turn(env, 2, "请先看新的输入", approve_only=True)
                    assert not core.store.one('SELECT 1 FROM events WHERE event_id=%s',
                                               (event["event_id"],))
                actual = await child(core, tmp_path, "poke", 2)
                if pre_ingress:
                    assert actual["calls"] == []
                    assert core.store.one("SELECT state FROM jobs WHERE kind='poke'") == {
                        "state": "QUEUED"}
                    assert core.store.rows("SELECT * FROM model_usage") == []
                else:
                    assert len(actual["calls"]) == 1, actual
                    assert {"lane": "poke", "progressed": True} in actual["turns"]
                    assert core.store.one(
                        "SELECT state,last_error_code FROM jobs WHERE kind='poke'",
                    ) == {"state": "SUCCEEDED", "last_error_code": None}
                    usage = core.store.rows("SELECT * FROM model_usage")
                    assert len(usage) == 1 and usage[0]["scenario"] == "qq_poke"
                    assert usage[0]["state"] == "SETTLED"
                    restarted = await child(core, tmp_path, "poke", 3)
                    assert restarted["calls"] == []
                    assert core.store.rows("SELECT * FROM model_usage") == usage
                assert core.store.one(
                    "SELECT job_id,authority_json FROM jobs WHERE kind='poke'",
                ) == original
                assert core.store.rows("SELECT * FROM outbox") == []  # Native decision was none.
    run(scenario())

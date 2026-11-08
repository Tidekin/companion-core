"""The remaining direct image path shares durable cloud admission and evidence."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from companion import cloud_budget
from companion.api import create_app
from companion.capabilities import snapshot
from companion.contracts import CoreError
from companion.core import Core
from tests.conftest import FakeModel, event, run
from tests.postgres_fixture import logical_state
from tests.support.content import enable, owner_share, png


def test_http_image_and_independent_reply_worker_share_the_cny_ledger(core):
    content, vision = enable(core)
    core.settings.content = content.options
    auth = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
    value = event(core, text="看到了什么？").model_dump(mode="json")
    value["payload"]["shared_content"] = [{"kind": "image", "slot": 0}]
    with TestClient(create_app(core.settings)) as client:
        active = client.app.state.core
        active.clock = active.content.clock = active.observations.clock = core.clock
        active.content.provider = vision
        assert client.post("/internal/v1/inbox/events", json=value, headers={
            **auth, "X-Idempotency-Key": "key-1",
        }).status_code == 202
        headers = {**auth, "Content-Type": "image/png", "X-Content-Key": "image:0"}
        path = "/internal/v2/content/evt_test_1/image"
        answer = client.post(path, content=png(), headers=headers)
        assert answer.status_code == 200, answer.text
        assert client.post(path, content=png(), headers=headers).json()["duplicate"]
    model = FakeModel()
    with Core(core.settings, model, core.clock) as worker:
        assert run(worker.run_once())
        assert "图片中有一只猫" in json.dumps(model.calls, ensure_ascii=False)
        rows = worker.store.rows("SELECT * FROM cloud_cost_snapshots")
        assert len(rows) == 2 and all(row["state"] == "SETTLED" for row in rows)
        assert worker.store.one("SELECT COUNT(*) n FROM outbox")["n"] == 1
        assert all(row["dispatched_at"] for row in rows)
        total = sum(row["amount_micro_cny"] for row in rows)
        assert total > 80
        policy = cloud_budget.policy(worker.store, worker.settings, worker.clock())
        cloud_budget.configure(worker.store, worker.settings, {
            "expected_version": policy["version"], "changes": {"daily_micro_cny": total},
        }, idempotency_key="reduce-to-consumed", now=worker.clock())
        next_id = owner_share(worker, 2, manifest=[{"kind": "image", "slot": 0}])
        worker.content.provider = vision
        run(worker.content.analyze_image(next_id, "image:0", png(), "image/png"))
        assert len(worker.store.rows("SELECT * FROM cloud_cost_snapshots")) == 3
        assert len(worker.store.rows("SELECT * FROM model_usage")) == 3
    assert len(vision.calls) == 2


def test_unknown_image_keeps_original_fx_and_blocks_replay_after_reconfiguration(core):
    service, vision = enable(core)
    vision.error = httpx.ReadTimeout("synthetic uncertainty")
    event_id = owner_share(core, manifest=[{"kind": "image", "slot": 0}])
    with pytest.raises(CoreError, match="CONTENT_VISION_UNCERTAIN"):
        run(service.analyze_image(event_id, "image:0", png(), "image/png"))
    before = core.store.one("SELECT * FROM cloud_cost_snapshots")
    assert before["state"] == "UNKNOWN" and before["amount_micro_cny"] == 80
    policy = cloud_budget.policy(core.store, core.settings, core.clock())
    cloud_budget.configure(core.store, core.settings, {
        "expected_version": policy["version"],
        "changes": {"usd_to_cny_micro": 7_000_000, "daily_micro_cny": 30_000_000},
    }, idempotency_key="new-price-policy", now=core.clock())
    with pytest.raises(CoreError, match="CONTENT_IMAGE_ALREADY_UNKNOWN"):
        run(service.analyze_image(event_id, "image:0", png(), "image/png"))
    after = core.store.one("SELECT * FROM cloud_cost_snapshots")
    assert after["snapshot_json"] == before["snapshot_json"]
    assert after["amount_micro_cny"] == 80 and after["state"] == "UNKNOWN"
    assert len(vision.calls) == 1


def test_runtime_capability_snapshot_treats_cloud_currency_as_advisory(core):
    core.settings.cloud_budget.daily_micro_cny = 0
    before = logical_state(core.store)
    with core.store.read_snapshot():
        value = snapshot(core.store, core.settings, core.clock)
    assert "CLOUD_DAILY_BUDGET_EXHAUSTED" not in json.dumps(value)
    assert logical_state(core.store) == before
    assert not core.store.rows("SELECT * FROM cloud_budget_policies")

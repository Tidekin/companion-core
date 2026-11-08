"""REM shares native turns and authority without owning the other nightly lanes."""

import json

import httpx
import pytest

from companion import owner_authority, owner_input, paid_budget, rem
from companion.autonomous_selector import native_selector
from companion.contracts import Candidate
from companion.model import ModelClient
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.rem_protocol import RemConfig
from tests.conftest import run
from tests.postgres_fixture import logical_state
from tests.support.autonomous_selector import drain
from tests.support.owner_authority import activate
from tests.support.owner_input import captured, inbound


def prepare(core, tmp_path, *, cap=50000, purposes=("memory", "rem")):
    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    core.ingest(inbound(core, capture), "rem-selector-source")
    core.store.execute("UPDATE jobs SET state='CANCELLED',lease_token=NULL")
    core.settings.memory_enabled = True
    core.settings.rem = RemConfig(enabled=True, local_hour=0, max_output_tokens=128,
                                  daily_micro_usd=cap, monthly_micro_usd=100000)
    original = owner_authority.current(core.store, core.settings, core.clock).policy
    policy = OwnerGrantPolicy.model_validate({
        **original.model_dump(), "purposes": list(purposes),
        "memory_scope": "owner_private" if "memory" in purposes else "none",
        "capability_policies": {"rem": paid_budget.sha(core.settings.rem.model_dump())}
        if "rem" in purposes else {},
    })
    activate(core, policy, revision=1, operation="rem-selector-grant")
    with core.store.transaction(control_write=True):
        memory_id = core.memory.add(Candidate(
            subject="user", kind="preference", fact_key="synthetic-rem-source",
            content="synthetic owner input", evidence_event_id=capture.event_id,
            evidence_quote="synthetic owner input",
        ), core.clock(), confirmed=True)
    assert memory_id
    return capture.event_id


@pytest.mark.parametrize("purposes", [("memory",), ("rem",)])
def test_rem_requires_both_independent_purposes_before_native_work(core, tmp_path, purposes):
    prepare(core, tmp_path, purposes=purposes)
    lane = next(x for x in native_selector(core, "rem-selector").lanes if x.name == "rem")
    before = logical_state(core.store)
    with core.store.read_snapshot():
        assert not run(lane.run()).progressed
    assert logical_state(core.store) == before
    assert core.store.rows("SELECT * FROM rem_records") == []


def test_rem_explicit_off_stays_silent_in_original_selector(core):
    assert not core.settings.rem.enabled
    turns = run(drain(native_selector(core, "rem-off")))
    assert {"diary", "consolidation", "memory", "rem"} <= {t.lane for t in turns}
    assert core.store.rows("SELECT * FROM rem_records") == []
    assert core.store.rows("SELECT * FROM model_usage") == []


@pytest.mark.parametrize("cap", [1, 50000])
def test_rem_currency_amount_does_not_block_next_lanes(core, tmp_path, cap):
    prepare(core, tmp_path, cap=cap)
    memories = core.store.rows("SELECT * FROM memories")
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "decision": "DREAM", "title": "想象", "text": "虚构的星空书房。",
                "questions": ["光谱为什么会变化？"],
            })}}], "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            core.model = ModelClient(core.settings, client)
            selector = native_selector(core, "rem-selector")
            index = next(i for i, lane in enumerate(selector.lanes) if lane.name == "rem")
            lane = selector.lanes[index]
            assert (await lane.run()).progressed  # Native job creation, no model yet.
            assert calls == []
            selector.cursor = index
            selector.resume()
            selected = await selector.step()
            assert selected.lane == "rem" and selected.result.progressed
            assert (await selector.step()).lane == "social"
            assert not (await lane.run()).progressed

    run(scenario())
    job = core.store.one("SELECT state,last_error_code FROM jobs WHERE kind='rem'")
    assert job == {"state": "SUCCEEDED", "last_error_code": None}
    assert len(calls) == 1
    assert core.store.one("SELECT state,scenario FROM model_usage") == {
        "state": "SETTLED", "scenario": "rem",
    }
    artifact = rem.records(core)[0]
    assert artifact["kind"] == "FICTION"
    assert artifact["questions"][0]["state"] == "UNVERIFIED"
    assert core.store.rows("SELECT * FROM memories") == memories
    assert core.store.rows("SELECT * FROM outbox") == []

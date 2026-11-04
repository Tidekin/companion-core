"""A later retained request must not make settled private history unusable."""

import json
from pathlib import Path

import httpx
import pytest

from companion import budget, model_carry, owner_input_state
from companion.contracts import CoreError
from companion.life_protocol import LifeShadowApproval
from companion.model import ModelClient
from companion.owner_authority_protocol import OwnerCapture, OwnerGrantPolicy
from tests.conftest import run
from tests.model_carry_support import MESSAGES
from tests.owner_model_fixtures import authorized_job
from tests.support.owner_authority import activate
from tests.test_model_renewal import renewal
from tests.test_model_renewal_native import life_case


def attempt(core, job, *, unknown=False):
    request = budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock())

    def provider(request):
        if unknown:
            raise httpx.ReadTimeout("synthetic dependent transport closed", request=request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "synthetic settled history"}}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 3},
        })

    async def complete():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            if unknown:
                with pytest.raises(CoreError, match="MODEL_RETRYABLE"):
                    await budget.complete(ModelClient(core.settings, client), core.store,
                        core.settings, request, MESSAGES, "chat", core.clock)
            else:
                await budget.complete(ModelClient(core.settings, client), core.store,
                    core.settings, request, MESSAGES, "chat", core.clock)

    run(complete())
    core.store.execute("UPDATE jobs SET state=? WHERE job_id=?",
                       ("FAILED" if unknown else "SUCCEEDED", job))
    return request


def history_case(core, tmp_path, monkeypatch):
    case = life_case(core, tmp_path, monkeypatch)
    primary = authorized_job(core, number=3)
    source = core.store.one("SELECT event_id FROM jobs WHERE job_id=?", (primary,))["event_id"]
    attempt(core, primary)
    dependent = authorized_job(core, number=2)
    core.store.execute("INSERT INTO job_sources(job_id,event_id) VALUES (?,?)", (dependent, source))
    current = attempt(core, dependent, unknown=True)
    return case, primary, source, dependent, current


def life_request(core, case, source):
    request, policy, witness = renewal(core, case)
    scope = LifeShadowApproval(
        scope="owner_private_life_shadow_v1", authorization_sha256=policy.authorization_sha256,
        conversation_id=core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
                                        (source,))["conversation_id"],
        source_event_ids=[source], purposes=["PRIVATE_MESSAGE_DRAFT"],
        automatic_actions=["qq.private.proactive"], expires_at=policy.expires_at, max_items=1,
    )
    policy = OwnerGrantPolicy.model_validate({**policy.model_dump(),
        "purposes": [*policy.purposes, "life"], "life_shadow": scope})
    request["successor_policy"] = policy.model_dump(mode="json")
    return request, policy, witness


def test_retained_closed_context_request_allows_settled_history_without_settling_input(
    core, tmp_path, monkeypatch,
):
    case, primary, source, dependent, current = history_case(core, tmp_path, monkeypatch)
    before = core.store.rows("SELECT * FROM model_usage ORDER BY request_id")
    capture = OwnerCapture.model_validate(json.loads(core.store.one(
        "SELECT value FROM runtime_meta WHERE key=?", ("owner_input:" + source,),
    )["value"])["capture"])
    state = owner_input_state.read(core.store, core.settings, capture)
    assert state["unsettled_model"] and not state["terminal"]
    request, policy, witness = life_request(core, case, source)
    receipt = model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    proof = model_carry.read(core.store, core.settings, request["authority"],
                            receipt["carry_sha256"], receipt["successor_policy_sha256"])
    dependent_source = core.store.one("SELECT event_id FROM jobs WHERE job_id=?",
                                      (dependent,))["event_id"]
    assert {item["event_id"] for item in proof["inputs"]} == {source, dependent_source}
    assert proof["request_ids"] == [current]
    assert proof["retained_micro_usd"] == next(
        row["billed_micro_usd"] for row in before if row["request_id"] == current)
    activate(core, policy, revision=4, operation="renew-closed-history-context")
    state = owner_input_state.read(core.store, core.settings, capture)
    assert state["unsettled_model"] and not state["terminal"]
    assert core.store.rows("SELECT * FROM model_usage ORDER BY request_id") == before
    original_job = core.store.one("SELECT state FROM jobs WHERE job_id=?", (primary,))
    assert original_job["state"] == "SUCCEEDED"


@pytest.mark.parametrize("change", ["own_unknown", "missing_primary"])
def test_retained_context_exception_keeps_original_input_responsibility_guard(
    core, tmp_path, monkeypatch, change,
):
    case, primary, source, dependent, _ = history_case(core, tmp_path, monkeypatch)
    if change == "own_unknown":
        source = core.store.one("SELECT event_id FROM jobs WHERE job_id=?",
                                (dependent,))["event_id"]
    else:
        core.store.execute("UPDATE jobs SET event_id=NULL WHERE job_id=?", (primary,))
    request, _, witness = life_request(core, case, source)
    journal = Path(core.settings.paid_model.journal_path)
    before = journal.read_bytes()
    with pytest.raises(CoreError, match="OWNER_MODEL_RENEWAL_LIFE_SOURCE_REQUIRED"):
        model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    assert journal.read_bytes() == before

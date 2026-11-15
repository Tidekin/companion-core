"""Signed owner HTTP input, SDK tools, restart, due selector and native publication."""

import asyncio
import json
import os
import pickle
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import owner_authority, paid_budget
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.reply_segments import SegmentedReplyConfig
from companion.store import after
from companion_delivery_protocol.native_chat import device_identity
from tests.conftest import run
from tests.sdk_reader_fixture import answer, call
from tests.support.native_chat import enroll, packet
from tests.support.owner_authority import activate
from tests.support.owner_authority_setup import prepare
from tests.support.owner_console import client as client


async def child(core, tmp_path, number, responses, job_id=None):
    config, output = tmp_path / f"care-{number}.pickle", tmp_path / f"care-{number}.json"
    config.write_bytes(
        pickle.dumps((core.settings, core.clock.now, job_id, responses, str(output)))
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-m",
        "tests.care_worker",
        str(config),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        logs, _ = await asyncio.wait_for(process.communicate(), 60)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, logs.decode(errors="replace")
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["pid"] != os.getpid()
    return result


@pytest.mark.parametrize("outcome", ["ask", "retry", "arrived", "erased"])
def test_signed_plan_survives_restart_and_only_unresolved_plan_sends(
    client,
    core,
    tmp_path,
    outcome,
):
    core.settings.memory_enabled = False
    core.settings.proactive_enabled = True
    core.settings.quiet_start = core.settings.quiet_end = 0
    core.settings.care.jitter_seconds = 0
    core.settings.segmented_reply = SegmentedReplyConfig(mode="semantic", max_segments=3)
    key = Ed25519PrivateKey.generate()
    policy = prepare(core, tmp_path)
    core.settings.paid_model = core.settings.paid_model.model_copy(update={"per_request_calls": 4})
    policy = policy.model_copy(
        update={"paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(core.settings))}
    )
    device_id, identity = device_identity(key.public_key().public_bytes_raw().hex())
    activate(
        core,
        OwnerGrantPolicy.model_validate(
            {
                **policy.model_dump(),
                "purposes": ["chat", "proactive", "history"],
                "capture": None,
                "history": {
                    "scope": "owner_private",
                    "not_before": after(core.clock(), -1),
                    "max_turns": 8,
                    "max_utf8_bytes": 6000,
                },
                "native_devices": {device_id: identity},
            }
        ),
    )
    headers, authority = enroll(client, key)
    response = client.post(
        "/v1/console/chat/messages",
        headers=headers,
        json=packet(core, key, authority, "我今天要出门，晚上才回家。"),
    )
    assert response.status_code == 202, response.text
    source = response.json()["event_id"]
    job = core.store.one("SELECT job_id FROM jobs WHERE event_id=%s", (source,))
    responses = [
        answer(calls=[call("clock", "current_time", {}), call("plans", "care_plan_list", {})]),
        answer(
            calls=[
                call(
                    "schedule",
                    "schedule_care",
                    {
                        "plan": {
                            "about": "晚上回家",
                            "quote": "晚上才回家",
                            "check_at": after(core.clock(), 3600),
                        }
                    },
                )
            ]
        ),
        answer("出门顺利，路上小心。"),
    ]
    if outcome == "retry":
        responses.insert(
            1,
            answer(
                calls=[
                    call(
                        "bad-quote",
                        "schedule_care",
                        {
                            "plan": {
                                "about": "晚上回家",
                                "quote": "这句话不在主人消息里",
                                "check_at": after(core.clock(), 3600),
                            }
                        },
                    )
                ]
            ),
        )
    first = run(child(core, tmp_path, 1, responses, job["job_id"]))
    calls = 4 if outcome == "retry" else 3
    assert len(first["requests"]) == calls
    if outcome == "retry":
        assert "CARE_QUOTE_UNSUPPORTED" in json.dumps(first["requests"][2]["messages"])
    plan = core.store.one("SELECT * FROM care_plans")
    assert plan["source_event_id"] == source and plan["state"] == "PENDING"
    assert {
        row["tool_name"]
        for row in core.store.rows("SELECT tool_name FROM agent_tool_calls WHERE state='RETURNED'")
    } == {"current_time", "care_plan_list", "schedule_care"}
    assert (
        core.store.one("SELECT count(*) AS n FROM model_usage WHERE state='SETTLED'")["n"] == calls
    )
    assert (
        owner_authority.current(
            core.store, core.settings, core.clock, purpose="proactive"
        ).authority.as_dict()
        == authority
    )
    if outcome == "arrived":
        core.clock.advance(1800)
        arrival = client.post(
            "/v1/console/chat/messages",
            headers=headers,
            json=packet(core, key, authority, "我已经到家了。"),
        )
        assert arrival.status_code == 202, arrival.text
        arrival_job = core.store.one(
            "SELECT job_id FROM jobs WHERE event_id=%s", (arrival.json()["event_id"],)
        )
        close = [
            answer(calls=[call("plans", "care_plan_list", {})]),
            answer(
                calls=[
                    call(
                        "close",
                        "close_care",
                        {
                            "closure": {
                                "plan_id": plan["plan_id"],
                                "expected_version": 1,
                                "quote": "已经到家了",
                            }
                        },
                    )
                ]
            ),
            answer("到家就好。"),
        ]
        run(child(core, tmp_path, 2, close, arrival_job["job_id"]))
        assert core.store.one("SELECT state FROM care_plans")["state"] == "CLOSED"
    elif outcome == "erased":
        with core.store.transaction(control_write=True):
            core.ledger.expire_events(core.store, [source], core.clock())
        assert core.store.one("SELECT state FROM care_plans")["state"] == "ERASED"
    core.clock.advance(3601)
    wording = "你现在到家了吗？\n\n路上还顺利吧。"
    due = run(child(core, tmp_path, 3, [answer(wording)] if outcome in {"ask", "retry"} else []))
    assert len(due["requests"]) == (1 if outcome in {"ask", "retry"} else 0)
    care_rows = core.store.rows(
        "SELECT * FROM outbox WHERE action_type='native.private.proactive' ORDER BY segment_index"
    )
    if outcome in {"ask", "retry"}:
        assert [json.loads(row["content_json"])["text"] for row in care_rows] == [
            "你现在到家了吗？\n\n",
            "路上还顺利吧。",
        ]
        assert all(row["state"] == "SENT" for row in care_rows)
        assert all(
            row["conversation_id"]
            == core.store.one("SELECT conversation_id FROM events WHERE event_id=%s", (source,))[
                "conversation_id"
            ]
            for row in care_rows
        )
    else:
        assert care_rows == []
    repeated = run(child(core, tmp_path, 4, []))
    assert repeated["requests"] == []

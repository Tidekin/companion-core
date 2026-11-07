"""Readiness projects the original, verified closure without settling liability."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from companion import budget, owner_authority
from companion.capabilities import open_unknown, public_source_readiness, snapshot
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop
from companion.retrieval import SemanticIndex
from companion.store import encode
from companion_onebot_bridge.config import BridgeConfig
from tests.conftest import run
from tests.model_carry_support import MESSAGES
from tests.owner_model_fixtures import authorized_job
from tests.sdk_reader_fixture import answer, call
from tests.support.bili_video_entrypoint import URL, reader_files, reader_once, upstream
from tests.support.carried_capabilities import reader_carry
from tests.support.core_http import real_core_http
from tests.support.model_carry_lifecycle import current_unknown
from tests.support.reading_followup import admit
from tests.test_native_bili_video import sdk_step


@pytest.mark.parametrize("later_unknown", [False, True])
def test_carried_unknown_does_not_block_reader_or_model_readiness(
    core, tmp_path, later_unknown,
):
    case = reader_carry(core, tmp_path)
    if later_unknown:
        current_unknown(core)
    journal = Path(core.settings.paid_model.journal_path)
    before = list(core.store.db.iterdump()), journal.read_bytes()
    ready = public_source_readiness(core.store, core.settings, core.clock, "bili")
    assert ready["state"] == "READY", ready
    rows = snapshot(core.store, core.settings, core.clock)["capabilities"]
    assert next(row for row in rows if row["id"] == "reading.bili")["state"] == "READY"
    assert not any("MODEL_PROVIDER_CONCURRENCY_EXHAUSTED" in row["reason_codes"] for row in rows)
    assert (list(core.store.db.iterdump()), journal.read_bytes()) == before
    assert core.store.one("SELECT state FROM model_usage WHERE request_id=?",
                          (case.request_id,))["state"] == "UNKNOWN"
    with pytest.raises(CoreError, match="REPLY_CANCELLED"):
        budget.reserve(core.store, core.settings, case.job_id, "chat", MESSAGES, core.clock())
    assert (list(core.store.db.iterdump()), journal.read_bytes()) == before


@pytest.mark.parametrize("boundary", ["unclosed", "stop", "journal"])
def test_unverified_or_unclosed_liability_keeps_reader_unavailable(core, tmp_path, boundary):
    case = reader_carry(core, tmp_path)
    if boundary == "unclosed":
        job = authorized_job(core, number=2)
        request = budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock())
        budget.settle(core.store, core.settings, request)
    elif boundary == "stop":
        owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
            operation_id="capabilities-stop", authority=case.successor["authority"]))
    else:
        journal = Path(core.settings.paid_model.journal_path)
        journal.write_bytes(journal.read_bytes() + b'{"broken":true}\n')
    ready = public_source_readiness(core.store, core.settings, core.clock, "bili")
    if boundary == "unclosed":
        assert ready["state"] == "READY", ready
        rows = snapshot(core.store, core.settings, core.clock)["capabilities"]
        assert any("MODEL_PROVIDER_CONCURRENCY_EXHAUSTED" in row["reason_codes"] for row in rows)
    else:
        assert ready["state"] != "READY", ready


def test_capability_window_limit_includes_carried_predecessor_usage(core, tmp_path):
    reader_carry(core, tmp_path, window_calls=1)
    rows = snapshot(core.store, core.settings, core.clock)["capabilities"]
    assert any("MODEL_WINDOW_BUDGET_EXHAUSTED" in row["reason_codes"] for row in rows)


@pytest.mark.parametrize("accounted", [False, True])
def test_signed_link_after_carry_reaches_independent_reader_and_sdk_context(
    core, tmp_path, monkeypatch, accounted,
):
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)
    case = reader_carry(core, tmp_path)
    if accounted:
        from companion import model_accounting
        from tests.support.model_accounting import prepared

        request, private = prepared(core, tmp_path, case.request_id)
        result = model_accounting.record(core.store, core.settings, request, private, core.clock)
        assert result["accounted_micro_usd"] > request["previous_accounted_micro_usd"]
    financial = core.store.one("SELECT * FROM model_usage WHERE request_id=?", (case.request_id,))
    config = BridgeConfig(owner_qq_id=core.settings.owner_qq_id,
        bot_qq_id=core.settings.bot_qq_id,
        bridge_token=core.settings.bridge_token.get_secret_value(),
        onebot_token="s" * 32, protocol_epoch=case.policy.capture.protocol_epoch)
    plan = SimpleNamespace(authority=case.successor["authority"], key=case.key, policy=case.policy)
    isolated = reader_files(tmp_path)
    metadata, _ = upstream(tmp_path)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + config.bridge_token},
        ) as client:
            _, accepted = await admit(client, core, config, plan, 2, "请看这集字幕 " + URL)
            job = accepted["job_id"]
            await sdk_step(core, tmp_path, job, [answer(calls=[
                call("carried-read", "read_content", {"url": URL})])], "RETRY_WAIT", "select")
            read = await reader_once(isolated, str(client.base_url), core.clock, metadata)
            assert read["remaining"] == 0
            requests = await sdk_step(core, tmp_path, job, [answer("字幕说恒星颜色与温度有关。")],
                                      "SUCCEEDED", "answer")
            assert "字幕说恒星颜色与温度有关" in encode(requests[0]["messages"])
            assert core.store.one("SELECT state FROM exploration_requests")["state"] == "DONE"
            assert (await reader_once(isolated, str(client.base_url), core.clock, []))[
                "requests"] == []

    run(scenario())
    assert core.store.one("SELECT * FROM model_usage WHERE request_id=?", (case.request_id,)) == (
        financial)


@pytest.mark.parametrize("later_unknown", [False, True])
def test_closed_unknown_does_not_block_the_provider_slot_retry(core, tmp_path, later_unknown):
    # fail_job waits out a provider-slot race unless an UNKNOWN still holds a slot;
    # journal-closed UNKNOWNs do not, exactly as admission and readiness count them.
    reader_carry(core, tmp_path)
    if later_unknown:
        current_unknown(core)
    assert core.store.one("SELECT 1 FROM model_usage WHERE state='UNKNOWN'")
    assert open_unknown(core.store, core.settings) is False


def test_unclosed_unknown_still_blocks_the_provider_slot_retry(core):
    now = core.clock()
    core.store.insert("model_usage", usage_id="usage_open", request_id="model_open",
                      scenario="chat", provider="test", model="test", input_tokens=0,
                      cached_input_tokens=0, output_tokens=0, billed_micro_usd=0,
                      is_estimate=1, occurred_at=now, state="UNKNOWN", pricing_known=1)
    assert open_unknown(core.store, core.settings) is True


def test_owner_authority_chat_waits_out_a_provider_slot_race(core, tmp_path):
    # Production runs OwnerAuthority without a live text window. A chat that lost the
    # single provider slot before admission must wait, not fail silently.
    reader_carry(core, tmp_path)
    job_id = authorized_job(core, number=3)
    job = core.store.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
    attempts = job["attempts"]
    core.fail_job(job, "MODEL_PROVIDER_CONCURRENCY_EXHAUSTED")
    assert core.store.one("SELECT state,attempts FROM jobs WHERE job_id=?", (job_id,)) == {
        "state": "RETRY_WAIT", "attempts": attempts - 1}
    assert core.store.rows("SELECT * FROM model_usage WHERE job_id=?", (job_id,)) == []

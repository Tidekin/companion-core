"""Synthetic P8 readiness checks; no external model, QQ account or paid I/O."""

import asyncio
import json

import httpx
import pytest

from companion import budget
from companion.contracts import CoreError, DeliveryResult, LeaseRequest
from companion.model import ModelClient, ModelResponse
from companion.transport import begin_send, delivery_result, lease_deliveries
from companion_onebot_bridge.app import receive
from companion_onebot_bridge.metrics import Metrics
from companion_onebot_bridge.spool import Spool
from tests.conftest import ingest, run
from tests.owner_model_fixtures import authorized_job
from tests.support.native_consumer_entry import environment
from tests.support.onebot_events import config, normalized, raw_event


def finish_reply(core, state):
    item = lease_deliveries(
        core.store, core.settings, LeaseRequest(worker_id="offline", limit=1), core.clock()
    )["items"][0]
    begin_send(core.store, item["message_id"], item["lease_token"], core.clock())
    delivery_result(
        core.store,
        core.settings,
        item["message_id"],
        DeliveryResult(lease_token=item["lease_token"], result=state),
        core.clock(),
    )


def test_stub_multiturn_context_keeps_sent_turn_and_excludes_uncertain_turn(core):
    ingest(core, 1, "第一轮：我们讨论春天")
    assert run(core.run_once())
    finish_reply(core, "SENT")
    ingest(core, 2, "第二轮：再讨论夏天")
    assert run(core.run_once())
    finish_reply(core, "DELIVERY_UNCERTAIN")
    ingest(core, 3, "第三轮：继续刚才的话题")
    assert run(core.run_once())
    messages, scenario = core.model.calls[-1]
    assert scenario == "chat"
    assert [row["content"] for row in messages[1:]] == [
        "第一轮：我们讨论春天",
        "回复：第一轮：我们讨论春天",
        "第三轮：继续刚才的话题",
    ]
    assert (
        core.store.one("SELECT COUNT(*) AS n FROM outbox WHERE state='DELIVERY_UNCERTAIN'")["n"]
        == 1
    )
    assert core.store.rows("SELECT * FROM memories") == []


def test_stub_context_overflow_drops_history_but_preserves_complete_current_input(core):
    ingest(core, 1, "old-history-marker" * 200)
    assert run(core.run_once())
    finish_reply(core, "SENT")
    current = "current-input-marker" * 500
    ingest(core, 2, current)
    assert run(core.run_once())
    messages = core.model.calls[-1][0]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": current}
    # This is deliberately a soft context bound: the complete current input survives.
    assert budget.token_upper_bound(messages) > core.settings.context_soft_tokens


def test_stub_sensitive_ingress_and_egress_are_redacted_before_storage(core):
    secret = "sk-SYNTHETIC_P8_SECRET_123456789"
    observed = []

    class SensitiveStub:
        async def complete(self, messages, scenario):
            observed.append((messages, scenario))
            assert core.store.one("SELECT state FROM model_usage")["state"] == "RESERVED"
            return ModelResponse("synthetic echo " + secret, 120, 30)

    core.model = SensitiveStub()
    ingest(core, 1, "请勿保存 " + secret + " password=SYNTHETIC_PASSWORD")
    assert run(core.run_once())
    evidence = json.dumps(
        {
            "messages": observed,
            "events": core.store.rows("SELECT body_json FROM events"),
            "outbox": core.store.rows("SELECT content_json FROM outbox"),
            "status": core.status(),
        }
    )
    assert secret not in evidence
    assert "SYNTHETIC_PASSWORD" not in evidence
    assert "[REDACTED]" in evidence
    assert core.store.rows("SELECT * FROM memories") == []


@pytest.mark.parametrize("failure", ["retryable", "crash"])
def test_unknown_reservation_is_retained_and_blocks_next_model_attempt(core, failure):
    ingest(core)
    if failure == "crash":
        job = core.claim_job("synthetic-crash")
        request_id = budget.reserve(
            core.store, core.settings, job["job_id"], "chat", core.context(job), core.clock()
        )
        initial = core.store.one(
            "SELECT billed_micro_usd FROM model_usage WHERE request_id=?", (request_id,)
        )["billed_micro_usd"]
        core.clock.advance(core.settings.lease_seconds + 1)
        expected_calls = 0
    else:
        core.model.error = CoreError("MODEL_RETRYABLE", 503)
        assert run(core.run_once())
        initial = core.store.one("SELECT billed_micro_usd FROM model_usage")["billed_micro_usd"]
        core.model.error = None
        core.clock.advance(6)
        expected_calls = 1
    assert initial > 0
    core.settings.monthly_limit_micro_usd = initial
    assert run(core.run_once())
    assert len(core.model.calls) == expected_calls
    assert core.store.one("SELECT state,billed_micro_usd FROM model_usage") == {
        "state": "UNKNOWN",
        "billed_micro_usd": initial,
    }
    assert core.store.one("SELECT last_error_code FROM jobs")["last_error_code"] == (
        "MONTHLY_BUDGET_EXHAUSTED"
    )
    assert core.store.one("SELECT COUNT(*) AS n FROM model_usage")["n"] == 1


@pytest.mark.parametrize("missing_usage", [False, True])
def test_model_adapter_payload_and_missing_usage_are_checked_with_stub_http(
    core, missing_usage, tmp_path
):
    from tests.test_paid_model_gate import approved_live

    approved_live(core, tmp_path, provider="deepseek", model=core.settings.model_name)
    requests = []

    def handler(request):
        requests.append(request)
        payload = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert payload == {
            "model": core.settings.model_name,
            "messages": [{"role": "user", "content": "synthetic request"}],
            "max_tokens": core.settings.max_output_tokens,
            "stream": False,
            "thinking": {"type": "disabled"},
        }
        body = {"choices": [{"message": {"content": "synthetic reply"}}]}
        if not missing_usage:
            body["usage"] = {
                "prompt_tokens": 50,
                "completion_tokens": 20,
                "prompt_cache_hit_tokens": 40,
            }
        return httpx.Response(200, json=body)

    async def scenario():
        core.settings.mode = "live"
        core.settings.model_url = "https://synthetic.invalid/v1"
        messages = [{"role": "user", "content": "synthetic request"}]
        request_id = budget.reserve(core.store, core.settings, authorized_job(core),
                                    "chat", messages, core.clock())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await budget.complete(
                ModelClient(core.settings, http),
                core.store,
                core.settings,
                request_id,
                messages,
                "chat",
                core.clock,
            )
        budget.settle(core.store, core.settings, request_id, result)
        return result

    response = run(scenario())
    assert len(requests) == 1
    if missing_usage:
        assert (response.input_tokens, response.output_tokens, response.cached_tokens) == (
            None,
            None,
            0,
        )
        usage = core.store.one("SELECT state,billed_micro_usd FROM model_usage")
        assert usage["state"] == "UNKNOWN" and usage["billed_micro_usd"] > 0
    else:
        assert (response.cached_tokens, response.input_tokens) == (40, 50)
        assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(401, json={"private": "SYNTHETIC_PROVIDER_SECRET"}), "MODEL_REJECTED"),
        (httpx.Response(200, json={"choices": []}), "MODEL_INVALID_RESPONSE"),
    ],
)
def test_model_adapter_errors_do_not_retry_or_expose_provider_body(core, response, code, tmp_path):
    from tests.test_paid_model_gate import approved_live

    approved_live(core, tmp_path, endpoint="https://synthetic.invalid")
    calls = []

    def handler(request):
        calls.append(request)
        return response

    async def scenario():
        async with environment(core, tmp_path, purposes=("chat",), paid_options={
            "endpoint": "https://synthetic.invalid"}) as (entry, _), httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as http:
            core.model = ModelClient(core.settings, http)
            await entry.admit(1, "检查单次模型失败")
            assert await core.run_once()
            core.clock.advance(30)
            assert not await core.run_once()

    run(scenario())
    assert len(calls) == 1
    assert core.store.one("SELECT state,last_error_code FROM jobs") == {
        "state": "FAILED",
        "last_error_code": code,
    }
    usage = core.store.one("SELECT state,billed_micro_usd FROM model_usage")
    assert usage["state"] == "UNKNOWN" and usage["billed_micro_usd"] > 0
    assert "SYNTHETIC_PROVIDER_SECRET" not in json.dumps(core.status())


def test_rejected_binary_and_expanding_text_do_not_block_following_frame(tmp_path, monkeypatch):
    cfg = config(data_dir=str(tmp_path / "synthetic-spool"))
    records = []

    async def scenario():
        stop, wake = asyncio.Event(), asyncio.Event()

        class SyntheticSocket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def __aiter__(self):
                yield b"\xff"
                yield json.dumps(raw_event(1, "token=x " * 2500))
                yield json.dumps(raw_event(2, "synthetic accepted frame")).encode()
                stop.set()

        async def connection(config, *, check=None):
            assert config == cfg
            assert check is None
            return SyntheticSocket()

        monkeypatch.setattr("companion_onebot_bridge.app.websocket", connection)
        with Spool(cfg) as spool:
            await receive(cfg, spool, wake, stop, Metrics("OFFLINE_FIXTURE", records.append))
            assert spool.pending_count == 1
            assert wake.is_set()
            body = spool.db.execute("SELECT body FROM inbox").fetchone()[0]
            assert "synthetic accepted frame" in body
            assert "token=x" not in body

    run(scenario())
    assert sum(row["stage"] == "event_rejected" for row in records) == 2
    assert "token=x" not in json.dumps(records)


def test_spool_disk_capacity_rejection_keeps_existing_inbox_atomic(tmp_path):
    cfg = config(data_dir=str(tmp_path / "synthetic-capacity"), max_disk_bytes=1048576)
    with Spool(cfg) as spool:
        first = normalized(raw_event(1))
        assert spool.put(first)
        # A workspace-local sparse file exercises the actual disk-size guard.
        with (spool.directory / "spool.sqlite3.synthetic-capacity").open("wb") as stream:
            stream.truncate(cfg.max_disk_bytes)
        with pytest.raises(ValueError, match="SPOOL_DISK_CAPACITY"):
            spool.put(normalized(raw_event(2)))
        assert spool.pending_count == 1
        assert not spool.put(first)
        assert spool.db.execute("SELECT key FROM inbox").fetchall() == [(first["event_id"],)]

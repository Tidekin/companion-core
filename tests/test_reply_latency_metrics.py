"""Separate complete reply groups and actual transport from local send preparation."""

import json

import httpx
import pytest

from companion_onebot_bridge import metrics
from companion_onebot_bridge.network import checked_http_client
from companion_onebot_bridge.onebot_client import bounded_post
from tests.conftest import run


def test_segment_metrics_keep_the_original_start_until_the_final_ack():
    records = []
    observed = metrics.Metrics("OFFLINE_FIXTURE", records.append)
    event = {"event_id": "evt_original", "trace_id": "trace_" + "a" * 32}
    observed.start(event, 1_000_000)
    for index, at in enumerate((11_000_000, 31_000_000, 51_000_000)):
        observed.delivered("reply:evt_original:part:" + str(index), "ack", at,
                           segment={"index": index, "count": 3})
        assert bool(observed.received) == (index < 2)
    spans = {row["stage"]: row["elapsed_ms"] for row in records if "elapsed_ms" in row}
    assert spans == {"t_bridge_send_ack": 10, "t_reply_group_ack": 50,
                     "t_reply_segments_ack": 40}
    assert not observed.received and not observed.first_acks


def test_uncertain_segment_never_emits_a_complete_group():
    records = []
    observed = metrics.Metrics("OFFLINE_FIXTURE", records.append)
    observed.start({"event_id": "evt_original", "trace_id": "trace_" + "a" * 32}, 0)
    observed.delivered("reply:evt_original:part:0", "ack", 1_000_000,
                       segment={"index": 0, "count": 2})
    observed.delivered("reply:evt_original:part:1", None, 2_000_000,
                       segment={"index": 1, "count": 2})
    assert [row["stage"] for row in records] == ["t_onebot_received", "t_bridge_send_ack"]
    assert not observed.received and not observed.first_acks


@pytest.mark.parametrize("failure", [None, "transport", "guard"])
def test_onebot_http_span_excludes_guard_and_includes_response_body(
    monkeypatch, capsys, failure,
):
    clock = [0]
    monkeypatch.setenv("COMPANION_TELEMETRY", "1")
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
    monkeypatch.setattr(metrics.time, "perf_counter_ns", lambda: clock[0])

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            clock[0] += 11_000_000
            yield b'{"status":"ok","retcode":0,"data":{"message_id":1}}'

    async def guard(request):
        clock[0] += 5_000_000
        if failure == "guard":
            raise ValueError("PRIVATE_GUARD_CANARY")

    async def delegate(request):
        clock[0] += 7_000_000
        if failure == "transport":
            raise httpx.ReadTimeout("PRIVATE_PROVIDER_CANARY", request=request)
        return httpx.Response(200, stream=Body())

    async def scenario():
        async with httpx.AsyncClient(base_url="http://private.invalid",
                                    transport=httpx.MockTransport(delegate)) as client:
            async with checked_http_client(client, guard) as checked:
                if failure:
                    with pytest.raises(ValueError if failure == "guard" else httpx.ReadTimeout):
                        await bounded_post(checked, "/send_private_msg", {"private": "CANARY"})
                else:
                    assert await bounded_post(checked, "/send_private_msg", {})

    run(scenario())
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    spans = [row for row in records if row["stage"] == "onebot_http"]
    assert len(spans) == int(failure != "guard")
    if spans:
        assert spans[0]["elapsed_ms"] == (7 if failure else 18)
        assert spans[0]["error_class"] == ("timeout" if failure else "none")
    assert "CANARY" not in json.dumps(records)


def test_nested_guard_refusal_does_not_claim_an_http_dispatch(monkeypatch, capsys):
    monkeypatch.setenv("COMPANION_TELEMETRY", "1")

    async def allowed(request):
        pass

    async def refused(request):
        raise ValueError("local refusal")

    async def scenario():
        transport = httpx.MockTransport(lambda _: pytest.fail("I/O"))
        async with httpx.AsyncClient(base_url="http://private.invalid",
                                    transport=transport) as c:
            async with checked_http_client(c, refused) as inner:
                async with checked_http_client(inner, allowed) as outer:
                    with pytest.raises(ValueError, match="local refusal"):
                        await bounded_post(outer, "/send_private_msg", {})

    run(scenario())
    assert not capsys.readouterr().out

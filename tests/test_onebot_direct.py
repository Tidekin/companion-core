"""Synthetic v2.0.3-shaped events; these are not live QQ evidence."""

import json

import httpx
import pytest

from companion.contracts import InboundEvent
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.support.onebot_events import config, normalized, raw_event


def test_owner_friend_message_has_stable_core_identity(core):
    incoming = normalized()
    assert incoming["conversation"]["platform_session_key"] == "onebot-direct:FriendMessage:10001"
    assert incoming["source_event_id"] == (
        "10002:onebot-direct:FriendMessage:10001:epoch:offline-first-20261020:42"
    )
    event = InboundEvent.model_validate(incoming)
    assert core.ingest(event, event.event_id)["duplicate"] is False
    assert core.ingest(InboundEvent.model_validate(normalized()), event.event_id)["duplicate"]


def test_expanding_redaction_is_rejected_before_it_can_poison_the_spool():
    with pytest.raises(ValueError, match="INVALID_TEXT_LENGTH"):
        normalized(raw_event(text="token=x " * 2500))


@pytest.mark.parametrize(
    "change",
    [
        {"post_type": "message_sent"},
        {"post_type": "notice"},
        {"message_type": "group"},
        {"sub_type": "group"},
        {"sub_type": "other"},
        {"user_id": 99999},
        {"self_id": 99999},
        {"sender": {"user_id": 99999}},
        {"sender": {}},
        {"group_id": 123},
        {"sender": {"user_id": 10001, "group_id": 123}},
        {"user_id": True},
        {"self_id": 10002.0},
        {"user_id": "010001"},
    ],
)
def test_non_owner_friend_events_never_reach_core(change):
    assert normalized(raw_event(**change)) is None


@pytest.mark.parametrize(
    "change",
    [
        {"message_id": None},
        {"message_id": True},
        {"message_id": "42/url"},
        {"time": True},
        {"time": 0},
        {"time": 999999999999},
        {"message": [{"type": "text", "data": {"text": 1}}]},
        {"message": [{"type": "face", "data": {}}] * 65},
        {"message": "a" * 20001},
        {"message": "bad[CQ:image,file=secret"},
    ],
)
def test_malformed_message_is_rejected_without_partial_text(change):
    with pytest.raises(ValueError):
        normalized(raw_event(**change))


def test_cq_and_array_media_are_never_interpreted_or_forwarded_as_urls():
    incoming = normalized(raw_event(message="你好[CQ:image,file=https://host/x?token=secret]"))
    assert "你好" in incoming["payload"]["text"]
    assert "尚未解析" in incoming["payload"]["text"]
    assert "https://" not in json.dumps(incoming)
    assert normalized(raw_event(message=[{"type": "image", "data": {"url": "secret"}}]))[
        "payload"
    ] == {"text": "", "media": [], "shared_content": []}


@pytest.mark.parametrize(
    "frame",
    [
        "[]",
        '{"post_type":"message","post_type":"notice"}',
        '{"x":' + "[" * 12 + "0" + "]" * 12 + "}",
        "x" * 65537,
        '{"bad":"\\ud800"}',
    ],
    ids=["not-object", "duplicate-key", "deep", "oversize", "surrogate"],
)
def test_raw_json_is_bounded_and_unambiguous(frame):
    with pytest.raises(ValueError):
        normalize(frame, config())


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_qq_id": "010001"},
        {"owner_qq_id": "10002"},
        {"protocol_epoch": ""},
        {"bridge_token": "short"},
        {"onebot_token": "synthetic-core-token-32-characters"},
        {"core_url": "https://public.example"},
        {"onebot_http_url": "http://169.254.169.254"},
        {"onebot_ws_url": "ws://snowluma/?access_token=secret"},
        {"max_pending": 0},
    ],
)
def test_unsafe_configuration_fails_closed(changes):
    with pytest.raises(ValueError):
        config(**changes)


def test_spool_restart_ack_dedupe_and_conflict(tmp_path):
    cfg = config(data_dir=str(tmp_path / "bridge"))
    event = normalized()
    with Spool(cfg) as spool:
        assert spool.put(event)
    requests = []

    def accepted(request):
        requests.append(request)
        return httpx.Response(
            202,
            json={
                "event_id": event["event_id"],
                "job_id": "job_synthetic",
                "duplicate": False,
                "state": "QUEUED",
            },
        )

    async def flush(spool):
        async with httpx.AsyncClient(
            base_url="http://core", transport=httpx.MockTransport(accepted)
        ) as client:
            assert await spool.flush_one(client)

    with Spool(cfg) as spool:
        assert spool.pending_count == 1
        run(flush(spool))
        assert spool.pending_count == 0
    with Spool(cfg) as spool:
        assert not spool.put(normalized())
        assert spool.pending_count == 0
        with pytest.raises(ValueError, match="CONFLICT"):
            spool.put(normalized(raw_event(text="changed")))
    assert len(requests) == 1
    with pytest.raises(ValueError, match="IDENTITY"):
        with Spool(config(data_dir=cfg.data_dir, protocol_epoch="another-epoch")):
            pass


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(202, json={}),
        httpx.Response(
            202,
            json={"event_id": "wrong", "job_id": "job_1", "state": "QUEUED", "duplicate": False},
        ),
    ],
)
def test_spool_never_discards_unconfirmed_acceptance(tmp_path, response):
    with Spool(config(data_dir=str(tmp_path / "spool"))) as spool:
        spool.put(normalized())

        async def attempt():
            async with httpx.AsyncClient(
                base_url="http://core", transport=httpx.MockTransport(lambda r: response)
            ) as client:
                with pytest.raises((ValueError, httpx.HTTPError)):
                    await spool.flush_one(client)

        run(attempt())
        assert spool.pending_count == 1


def test_spool_enforces_single_instance_and_capacity(tmp_path):
    cfg = config(data_dir=str(tmp_path / "spool"), max_pending=1)
    with Spool(cfg) as spool:
        with pytest.raises(RuntimeError, match="ALREADY_RUNNING"):
            with Spool(cfg):
                pass
        spool.put(normalized())
        assert not spool.put(normalized())
        with pytest.raises(ValueError, match="CAPACITY"):
            spool.put(normalized(raw_event(43)))


@pytest.mark.parametrize(
    "body",
    [
        {"error": "OTHER"},
        {"error": "EVENT_ERASED", "extra": True},
        {"error": "EVENT_ERASED", "padding": "x" * 65536},
    ],
    ids=["wrong-error", "extra-field", "oversize"],
)
def test_untrusted_erasure_receipt_cannot_clear_pending_text(tmp_path, body):
    with Spool(config(data_dir=str(tmp_path / "bridge"))) as spool:
        payload = normalized()
        spool.put(payload)

        async def attempt():
            async with httpx.AsyncClient(
                base_url="http://core",
                transport=httpx.MockTransport(lambda r: httpx.Response(410, json=body)),
            ) as client:
                with pytest.raises(ValueError):
                    await spool.flush_one(client)

        run(attempt())
        assert spool.pending_count == 1
        assert spool.db.execute("SELECT body FROM inbox").fetchone()[0] is not None


def test_abrupt_process_death_preserves_fsynced_inbox_and_releases_lock(tmp_path):
    import os
    import subprocess
    import sys

    path = str(tmp_path / "crash-spool")
    child = """
import json, os, sys
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.spool import Spool
cfg = BridgeConfig(owner_qq_id='10001', bot_qq_id='10002',
    bridge_token='SYNTHETIC_CORE_TOKEN_32_CHARACTERS',
    onebot_token='SYNTHETIC_ONEBOT_TOKEN_32_CHARACTERS',
    protocol_epoch='offline-first-20261020', data_dir=sys.argv[1])
spool = Spool(cfg)
spool.put(json.loads(sys.argv[2]))
os._exit(73)
"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("COMPANION_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", child, path, json.dumps(normalized())],
        env=env,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 73
    with Spool(config(data_dir=path)) as spool:
        assert spool.pending_count == 1
        assert not spool.put(normalized())

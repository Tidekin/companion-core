import copy
import json
from datetime import UTC, datetime, timedelta

import httpx

import companion_delivery_protocol as wire
from companion_onebot_bridge import journal, owner_handoff
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool, encode
from tests.support.onebot_events import raw_event


def state(capture, *, terminal=False):
    return {key: capture[key] for key in ("authority", "event_id", "input_sha256")} | {
        "reply_revision": 1, "terminal": terminal,
        "state": "CANCELLED" if terminal else "PENDING", "unsettled_work": False,
        "unsettled_model": not terminal, "unsettled_delivery": False, "work_sha256": "1" * 64}


def capture(config, number=1):
    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        now = runtime.clock()
        frame = encode(raw_event(number, text="synthetic original", time=int(now.timestamp()),
            self_id=int(config.bot_qq_id), user_id=int(config.owner_qq_id),
            sender={"user_id": int(config.owner_qq_id)}))
        event = normalize(frame, config, now=now.timestamp())
        runtime.persist_capture(event, frame, received_at=now, peer=("127.0.0.1", 3001))
        return json.loads(spool.db.execute("SELECT capture_json FROM window_inputs "
                                           "WHERE event_id=?",
                                           (event["event_id"],)).fetchone()[0])


def proof(capture):
    value = {"kind": "owner_model_unknown_carry_v1", "version": 1, "state": "PREPARED",
        "authority": capture["authority"], "core_stop_sha256": "e" * 64,
        "budget_journal_id": "budget_" + "f" * 32, "carry_sha256": "a" * 64,
        "record_sha256": "b" * 64, "successor_policy_sha256": "c" * 64,
        "successor_window_id": "synthetic_successor", "retained_micro_usd": 84,
        "closed_executors_sha256": "d" * 64, "request_ids": ["model_" + "1" * 32],
        "inputs": [{"event_id": capture["event_id"], "input_sha256": capture["input_sha256"],
            "work_sha256": state(capture)["work_sha256"], "request_ids": ["model_" + "1" * 32]}]}
    return seal(value)


def seal(value):
    value = copy.deepcopy(value)
    value["receipt_sha256"] = wire.digest({k: v for k, v in value.items() if k != "receipt_sha256"})
    return value


def handler(config, retained, *, states=None, mutate=None, calls=None,
            credential="synthetic-bridge"):
    successor = config.owner_authority | {"revision": config.owner_authority["revision"] + 2,
                                          "policy_sha256": retained["successor_policy_sha256"]}
    counts = {}

    def respond(request):
        assert request.headers["Authorization"] == "Bearer " + credential
        path = request.url.path
        counts[path] = counts.get(path, 0) + 1
        if calls is not None:
            calls.append(path)
        body = json.loads(request.content)
        if path.endswith("/stop-receipt"):
            assert body == {"authority": config.owner_authority}
            result = {"authority": config.owner_authority, "state": "STOPPED",
                "stopped_authority": config.owner_authority | {
                    "revision": config.owner_authority["revision"] + 1},
                "core_stop_sha256": "e" * 64, "budget_journal_id": "budget_" + "f" * 32,
                "quiescence_proven": False}
        elif path.endswith("/model-carry-receipt"):
            assert body == {"authority": config.owner_authority,
                "carry_sha256": "a" * 64, "successor_policy_sha256": "c" * 64}
            result = copy.deepcopy(retained)
        elif path.endswith("/inputs/state"):
            result = copy.deepcopy((states or {}).get(body["event_id"], state(body)))
        elif path.endswith("/check"):
            assert body == {"authority": successor, "purpose": "chat"}
            result = {"approved": True, "authority": successor, "purpose": "chat",
                "expires_at": None, "budget_journal_id": "budget_" + "f" * 32,
                "capability_policies": {}, "capture": {"public_key": config.capture_public_key,
                    "identity_sha256": journal.digest([config.owner_qq_id, config.bot_qq_id]),
                    "protocol_epoch": config.protocol_epoch}}
        else:
            raise AssertionError(path)
        if mutate:
            result = mutate(path, counts[path], result)
        return httpx.Response(200, json=result)
    return respond


def client(respond):
    return httpx.AsyncClient(base_url="http://127.0.0.1:8765", trust_env=False,
        headers={"Authorization": "Bearer synthetic-bridge"},
        transport=httpx.MockTransport(respond))


def stopped(config):
    with Spool(config) as spool:
        OwnerRuntime(spool).stop()
    spool = Spool(config, reconciliation_only=True)
    return OwnerRuntime(spool, clock=lambda: datetime.now(UTC) + timedelta(seconds=1))


async def prepare(runtime, core):
    return await owner_handoff.prepare_carry(runtime, core, "a" * 64, "c" * 64)

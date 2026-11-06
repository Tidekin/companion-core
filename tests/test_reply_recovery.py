"""Native partial-reply recovery through real HTTP/WS and independent producers."""

import json
from dataclasses import asdict, replace

import pytest
from pydantic import SecretStr

from companion import owner_authority
from companion.owner_authority_protocol import AuthorityStop
from companion.reply_segments import SegmentedReplyConfig
from companion.transport import recover_deliveries
from companion_onebot_bridge import owner_handoff
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.owner_send import OwnerSendGuard
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.native_continuous_support import frame, host
from tests.test_bridge_owner_resolution import child
from tests.test_evaluation_process import process_config

PARTS = ["synthetic first\n\n", "synthetic second\n\n", "synthetic third\n\n", "synthetic last"]
ORIGIN = "https://console.example.test"


@pytest.mark.parametrize("interrupt", [False, True])
def test_console_copies_only_unstarted_suffix_and_preserves_original_ack(
    core,
    tmp_path,
    interrupt,
):
    core.settings.control_token = SecretStr("synthetic-owner-control-token")
    core.settings.console.enabled = True
    core.settings.console.public_origin = ORIGIN
    core.settings.segmented_reply = SegmentedReplyConfig(
        mode="semantic", max_segments="unlimited", interval_ms=0
    )

    async def source(ws):
        await ws.send(frame(1, text="synthetic partial reply"))
        await ws.wait_closed()

    async def scenario():
        async with host(core, tmp_path, source, ticking=False, purposes=("chat", "history")) as (
            active,
            _,
            sends,
            _,
        ):
            await active.capture()
            await active.flush()
            worker = tmp_path / "worker.json"
            process_config(core, worker)
            assert await child(
                "tests.owner_runtime_process",
                "--config",
                str(worker),
                "--now",
                core.clock(),
                "--reply",
                "".join(PARTS),
            ) == {"worked": True, "calls": 1}
            assert await active.pump()
            originals = core.store.rows("SELECT * FROM outbox ORDER BY segment_index")
            assert len(originals) == 4 and originals[0]["state"] == "SENT"
            prefix = originals[0]
            ack = active.db.execute(
                "SELECT * FROM window_attempts WHERE message_id=?", (prefix["message_id"],)
            ).fetchone()
            leased = await active.client.post(
                "/internal/v1/deliveries/lease",
                json={"worker_id": "bridge", "authority": active.runtime.authority, "limit": 1},
            )
            assert leased.status_code == 200, leased.text
            item = leased.json()["items"][0]
            OwnerSendGuard(active.runtime, active.client).reserve(item)
            config = active.config
            active.spool.close()
            core.clock.advance(core.settings.lease_seconds + 1)
            with core.store.transaction():
                recover_deliveries(core.store, core.clock())
            stopped = owner_authority.stop(
                core.store,
                core.settings,
                core.clock,
                AuthorityStop(
                    authority=config.owner_authority, operation_id="stop-synthetic-reply"
                ),
            )
            cfg_path = tmp_path / "bridge.json"
            cfg_path.write_text(json.dumps(asdict(config)), encoding="utf-8")
            args = (
                "--config",
                str(cfg_path),
                "--now",
                core.clock(),
                "--message-id",
                item["message_id"],
            )
            assert (await child("tests.owner_reconcile_process", *args, "--operation", "prepare"))[
                "state"
            ] == "NOT_SENT"
            assert (
                await child("tests.owner_reconcile_process", *args, "--operation", "reconcile")
            )["state"] == "NOT_SENT"
            with Spool(config, reconciliation_only=True) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                assert (await owner_handoff.prepare(runtime, active.client))[
                    "state"
                ] == "HANDOFF_PREPARED"
                old = owner_authority.evidence(core.store, core.settings, config.owner_authority)
                from tests.support.owner_authority import activate

                successor = activate(
                    core,
                    old.policy,
                    revision=stopped["authority"]["revision"],
                    operation="activate-synthetic-reply",
                )[0]["authority"]
                assert (await owner_handoff.handoff(runtime, active.client, successor))[
                    "state"
                ] == "ACTIVE"
            active.config = replace(config, owner_authority=successor)
            active.spool = Spool(active.config)
            active.runtime = OwnerRuntime(active.spool, clock=lambda: core.clock.now)
            before = core.store.rows("SELECT * FROM outbox ORDER BY segment_index")
            usage = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
            login = await active.client.post(
                "/v1/console/session",
                json={"password": "synthetic-owner-control-token"},
                headers={"Origin": ORIGIN},
            )
            assert login.status_code == 200, login.text
            headers = {
                "Origin": ORIGIN,
                "X-CSRF-Token": login.json()["csrf_token"],
                "Cookie": "companion_owner_session=" + login.cookies["companion_owner_session"],
                "Idempotency-Key": "synthetic-copy-suffix",
            }
            path = "/v1/console/deliveries/" + item["message_id"] + "/recovery"
            assert (await active.client.get(path)).status_code == 401
            receipt_key = "owner_delivery_not_sent:" + item["message_id"]
            retained = core.store.one("SELECT value FROM runtime_meta WHERE key=?", (receipt_key,))
            with core.store.transaction():
                core.store.execute("DELETE FROM runtime_meta WHERE key=?", (receipt_key,))
            assert (await active.client.get(path, headers=headers)).status_code == 409
            with core.store.transaction():
                core.store.insert("runtime_meta", key=receipt_key, value=retained["value"])
                core.store.execute("UPDATE outbox SET attempt_count=1 WHERE message_id=?",
                                   (item["message_id"],))
            assert (await active.client.get(path, headers=headers)).status_code == 409
            with core.store.transaction():
                core.store.execute("UPDATE outbox SET attempt_count=0 WHERE message_id=?",
                                   (item["message_id"],))
            preview = await active.client.get(path, headers=headers)
            assert preview.status_code == 200, preview.text
            selected = preview.json()
            assert selected["texts"] == PARTS[1:]
            assert selected["skip_message_ids"] == [prefix["message_id"]]
            body = {"expected_sha256": selected["expected_sha256"]}
            rejected = await active.client.post(
                path,
                json={"expected_sha256": "0" * 64},
                headers=headers | {"Idempotency-Key": "synthetic-stale-selection"},
            )
            assert rejected.status_code == 409
            assert not core.store.rows("SELECT * FROM jobs WHERE kind='reply_recovery'")
            created = await active.client.post(path, json=body, headers=headers)
            assert created.status_code == 200, created.text
            assert created.json() == {"state": "QUEUED"}
            with core.store.transaction():
                core.store.execute("UPDATE console_operations SET state='STARTED',result_json=NULL "
                                   "WHERE request_id=?", (headers["Idempotency-Key"],))
            assert (
                await active.client.post(path, json=body, headers=headers)
            ).json() == created.json()
            duplicate = await active.client.post(
                path, json=body, headers=headers | {"Idempotency-Key": "synthetic-other-copy"}
            )
            assert duplicate.status_code == 409
            assert len(core.store.rows("SELECT * FROM jobs WHERE kind='reply_recovery'")) == 1
            assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid") == usage
            assert (
                core.store.rows(
                    "SELECT * FROM outbox WHERE message_id IN (?,?,?,?) ORDER BY segment_index",
                    tuple(r["message_id"] for r in before),
                )
                == before
            )
            assert await active.pump()
            if interrupt:
                from companion.privacy import advance_epoch

                with core.store.transaction():
                    advance_epoch(core.store)
                assert not await active.pump()
                assert len(sends) == 2
                assert not core.store.one(
                    "SELECT 1 FROM outbox WHERE state IN ('PENDING','LEASED')"
                )
            else:
                assert await active.pump()
                active.restart()
                assert await active.pump()
                assert not await active.pump()
                assert len(sends) == 4
                texts = [r["message"][0]["data"]["text"] for r in sends]
                assert texts == [p.strip() for p in PARTS]
                from companion.dialogue_history import recent_sent_sources

                history = recent_sent_sources(core.store, originals[0]["conversation_id"], "", 10,
                                              cutoff_rowid=100000)
                assert len(history) == 1 and history[0]["reply_text"] == "".join(PARTS)
            assert (
                core.store.one("SELECT * FROM outbox WHERE message_id=?", (prefix["message_id"],))
                == prefix
            )
            assert (
                active.db.execute(
                    "SELECT * FROM window_attempts WHERE message_id=?", (prefix["message_id"],)
                ).fetchone()
                == ack
            )
            assert active.runtime.row()["state"] == "ACTIVE"
            active.runtime.verify()
            assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid") == usage
            # The dedicated producer keeps delivery liability in the existing
            # original-input completion receipt after the explicit operation.
            captured = json.loads(
                active.db.execute(
                    "SELECT capture_json FROM window_inputs ORDER BY rowid LIMIT 1"
                ).fetchone()[0]
            )
            from companion.owner_authority_protocol import OwnerCapture
            from companion.owner_input_state import read

            assert read(core.store, core.settings, OwnerCapture.model_validate(captured))[
                "terminal"
            ]

    run(scenario())


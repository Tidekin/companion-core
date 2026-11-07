"""Original restore and legal policy-redaction refusal through the new accounting host."""

import asyncio
import copy
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from companion_onebot_bridge import meme_accounting_drain as drain
from tests.conftest import run
from tests.support.meme_accounting_drain import stopped_case
from tests.support.meme_responsibility import snapshot


@pytest.mark.parametrize("kind", ["stale_snapshot", "restored_backup"])
def test_actual_latest_ledger_and_restore_barrier_cannot_be_cleared_by_drain(
    core, tmp_path, monkeypatch, kind
):
    from companion.backup import backup, restore
    from companion.store import Store
    from tests.support.core_http import real_core_http

    async def scenario():
        async with stopped_case(core, tmp_path, monkeypatch) as c:
            old, encrypted, key = (
                tmp_path / "old.sqlite3",
                tmp_path / "old.backup",
                Fernet.generate_key(),
            )
            if kind == "stale_snapshot":
                with closing(sqlite3.connect(old)) as destination:
                    core.store.db.backup(destination)
            else:
                backup(core.settings.database, encrypted, key)
            with core.store.transaction():
                core.ledger.expire_events(core.store, [c.context["event_id"]], core.clock())
            if kind == "restored_backup":
                restore(encrypted, old, key, core.settings.erasure_ledger)
            with closing(Store(str(old))) as store:
                shadow = copy.copy(core)
                shadow.store = store
                shadow.settings = core.settings.model_copy(update={"database": str(old)})
                before = snapshot(shadow)
                with closing(sqlite3.connect(c.database)) as db:
                    bridge = tuple(db.iterdump())
                async with real_core_http(shadow) as port:
                    config = replace(c.config, core_url=f"http://127.0.0.1:{port}")
                    report = await drain.command(config, SimpleNamespace())
                assert report["state"] == "UNVERIFIED" and report["updated"] == 0
                assert report["attempted"] == report["unverified"] == 1
                assert snapshot(shadow) == before
                with closing(sqlite3.connect(c.database)) as db:
                    assert tuple(db.iterdump()) == bridge

    run(scenario())


def test_original_reconciled_v2_then_legally_redacted_policy_is_still_refused(
    core, tmp_path, monkeypatch
):
    from companion import delivery_reconciliation, owner_authority, owner_delivery
    from companion.contracts import DeliveryResult, LeaseRequest
    from companion.life_protocol import LifeShadowApproval
    from companion.owner_authority_protocol import AuthorityStop, OwnerSendCheck
    from companion.store import after
    from companion.transport import begin_send, delivery_result, lease_deliveries
    from tests import meme_authority_fixture
    from tests.conftest import ingest
    from tests.meme_authority_fixture import admit
    from tests.support.meme_core import worker
    from tests.support.meme_responsibility_bridge import bridge_case
    from tests.support.owner_delivery_reconciliation import delivery_proof

    core.clock.now = datetime.now(UTC).replace(microsecond=0)
    ingest(core, number=99, origin="synthetic-policy-source:FriendMessage:10001")
    source = core.store.one("SELECT event_id,conversation_id FROM events")
    core.store.execute("UPDATE jobs SET state='CANCELLED'")
    scope = LifeShadowApproval(
        scope="owner_private_life_shadow_v1",
        authorization_sha256="b" * 64,
        conversation_id=source["conversation_id"],
        source_event_ids=[source["event_id"]],
        purposes=["PRIVATE_DIARY"],
        expires_at=after(core.clock(), 3600),
        max_items=2,
    )
    original_policy = meme_authority_fixture.OwnerGrantPolicy

    def policy(**values):
        # Configure the original test grant before real native preview/activation.
        return original_policy(
            **{**values, "purposes": [*values["purposes"], "life"], "life_shadow": scope}
        )

    monkeypatch.setattr(meme_authority_fixture, "OwnerGrantPolicy", policy)

    async def scenario():
        async with bridge_case(core, tmp_path, monkeypatch) as c:
            from companion_onebot_bridge.meme_runtime import MemeRuntime

            MemeRuntime(c.owner)
            assert await worker(core, [])
            event, accepted = await admit(
                c.core_client, c.owner.config, c.grant, c.owner.clock(), 2
            )
            assert accepted.status_code == 202, accepted.text
            from companion_meme_protocol import OFFER_WAIT_SECONDS

            core.clock.advance(OFFER_WAIT_SECONDS + 1)
            # Real independent chat worker with Mock provider; no OneBot transport.
            worked = await worker(core, [])
            if not worked:
                diagnostic = {"jobs": core.store.rows(
                    "SELECT kind,state,last_error_code,next_attempt_at FROM jobs"),
                    "usage": core.store.rows("SELECT scenario,state FROM model_usage"),
                    "outbox": core.store.rows("SELECT state FROM outbox")}
                raise AssertionError(json.dumps(diagnostic))
            item = lease_deliveries(
                core.store,
                core.settings,
                LeaseRequest(worker_id="synthetic-owner", authority=c.grant.authority),
                core.clock(),
            )["items"][0]
            check = OwnerSendCheck(
                authority=item["authority"],
                message_id=item["message_id"],
                event_id=item["origin_event_id"],
                content_sha256=item["content_sha256"],
                lease_token=item["lease_token"],
                reply_revision=item["reply_revision"],
            )
            begin_send(
                core.store,
                item["message_id"],
                item["lease_token"],
                core.clock(),
                core.settings,
                clock=core.clock,
            )
            owner_delivery.send_check(core.store, core.settings, core.clock, check)
            outcome = DeliveryResult(lease_token=item["lease_token"], result="DELIVERY_UNCERTAIN")
            delivery_result(core.store, core.settings, item["message_id"], outcome, core.clock())
            stopped = owner_authority.stop(
                core.store,
                core.settings,
                core.clock,
                AuthorityStop(
                    operation_id="accounting-redaction-stop", authority=c.grant.authority
                ),
            )
            capture = SimpleNamespace(authority=c.grant.authority, event_id=event["event_id"])
            proof = delivery_proof(core, capture, c.grant.key, item, check, outcome, stopped)
            assert (
                delivery_reconciliation.prepare(core.store, core.settings, proof, core.clock())[
                    "state"
                ]
                == "READY"
            )
            delivery_reconciliation.resolve(
                core.store,
                core.settings,
                item["message_id"],
                "CANCELLED",
                core.clock(),
                operation_id="accounting-redaction-resolution",
                operation_sha256="e" * 64,
            )
            with core.store.transaction():
                core.ledger.expire_events(core.store, [source["event_id"]], core.clock())
            assert (
                core.store.one(
                    "SELECT policy_redacted FROM owner_authority_revisions WHERE revision=1"
                )["policy_redacted"]
                == 1
            )
            text = await asyncio.to_thread(
                Path(core.settings.paid_model.journal_path).read_text, encoding="utf-8"
            )
            records = [json.loads(line) for line in text.splitlines()]
            assert any(
                r.get("kind") == delivery_reconciliation.KIND and r["quiescence"]["version"] == 2
                for r in records
            )
            refused = await c.core_client.post(
                "/internal/v1/memes/captions/" + c.job_id + "/responsibility", json={}
            )
            assert refused.status_code == 503, refused.text
            assert refused.json()["error"] == "MODEL_BUDGET_RECONCILIATION_REQUIRED"
            c.owner.stop()
            database, config = c.owner.spool.directory / "spool.sqlite3", c.owner.config
            c.owner.spool.close()
            before = snapshot(core)
            with closing(sqlite3.connect(database)) as db:
                original = tuple(db.iterdump())
            report = await drain.command(config, SimpleNamespace())
            assert report["state"] == "UNVERIFIED" and report["unverified"] == 1
            assert report["updated"] == 0 and snapshot(core) == before
            with closing(sqlite3.connect(database)) as db:
                assert tuple(db.iterdump()) == original

    run(scenario())

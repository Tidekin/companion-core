"""Known-caption private reads preserve original finance and erase custody."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from psycopg import sql

from companion import meme_responsibility, meme_service
from companion.postgres import Database
from companion.store import after
from companion_meme_protocol import digest
from tests.conftest import run
from tests.meme_authority_fixture import admit, notice, stop
from tests.postgres_fixture import copy_database, logical_state
from tests.support.meme_core import body, caption_environment, worker
from tests.support.meme_responsibility import liability_case, snapshot
from tests.support.meme_service import claim, reservation, result


async def unchanged_read(core, c, *, status=200):
    before = snapshot(core)
    response = await c.client.post(c.path, json={})
    assert response.status_code == status, response.text
    assert snapshot(core) == before
    return response.json()


def test_actual_settled_caption_private_read_is_metadata_only_and_read_only(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, _, client, _, _, now, attempt = values
            payload = body(core, event, attempt, now)
            admitted = await client.post("/internal/v1/memes/captions", json=payload)
            assert admitted.status_code == 200
            job_id = admitted.json()["job_id"]
            calls = []
            assert await worker(core, calls) and len(calls) == 1
            original = core.store.one("SELECT * FROM model_usage")
            before = snapshot(core)
            response = await client.post(
                "/internal/v1/memes/captions/" + job_id + "/responsibility", json={})
            assert response.status_code == 200, response.text
            value = response.json()
            assert response.headers["Cache-Control"] == "no-store"
            assert value["liability_kind"] == "USAGE" and value["job_state"] == "SUCCEEDED"
            assert value["usage_owner_job_id"] == job_id
            assert value["usage"]["usage_request_id"] == original["request_id"]
            assert value["usage"]["state"] == "SETTLED"
            assert value["request_sha256"] == admitted.json()["request_sha256"]
            assert value["event_id"] == event["event_id"] and not value["body_erased"]
            assert not {"result", "context", "source_evidence", "data_base64"} & value.keys()
            assert "小猫" not in response.text and "https://" not in response.text
            assert snapshot(core) == before

    run(scenario())


def test_actual_dispatch_loss_retains_unknown_and_original_fee_owner(core, tmp_path, monkeypatch):
    async def scenario():
        async with liability_case(core, tmp_path, monkeypatch) as c:
            calls = []

            async def lost():
                raise ValueError("Synthetic response lost after actual dispatch")

            assert await worker(core, calls, hook=lost) and len(calls) == 1
            value = await unchanged_read(core, c)
            assert value["job_state"] == "FAILED" and value["liability_kind"] == "USAGE"
            assert value["usage"]["state"] == "UNKNOWN"
            assert value["usage"]["billed_micro_usd"] is None
            assert value["usage_owner_job_id"] == c.job_id

    run(scenario())


def test_actual_reserved_admission_read_does_not_dispatch_or_finish_the_original_model(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with liability_case(core, tmp_path, monkeypatch) as c:
            entered, release = asyncio.Event(), asyncio.Event()

            async def held():
                entered.set()
                await release.wait()

            calls = []
            task = asyncio.create_task(worker(core, calls, hook=held))
            try:
                async with asyncio.timeout(5):
                    await entered.wait()
                value = await unchanged_read(core, c)
                assert value["job_state"] == "RUNNING" and value["liability_kind"] == "USAGE"
                assert value["usage"]["state"] == "RESERVED"
                assert value["usage_owner_job_id"] == c.job_id and len(calls) == 1
            finally:
                release.set()
                assert await task

    run(scenario())


def test_call_quota_refusal_has_terminal_no_admission_despite_a_preallocated_uid(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with liability_case(core, tmp_path, monkeypatch, daily_calls=0) as c:
            calls = []
            assert await worker(core, calls) and not calls
            assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (c.job_id,)) == {
                "state": "FAILED"}
            assert core.store.one("SELECT usage_request_id FROM meme_captions")["usage_request_id"]
            assert not core.store.rows("SELECT * FROM model_usage")
            assert not core.store.rows("SELECT * FROM model_admissions")
            value = await unchanged_read(core, c)
            assert value["liability_kind"] == "NO_ADMISSION"
            assert value["usage"] is None and value["usage_owner_job_id"] is None

    run(scenario())


@pytest.mark.parametrize("state", ["QUEUED", "RUNNING", "RETRY_WAIT", "SUCCEEDED"])
def test_unfunded_nonterminal_or_success_state_cannot_claim_no_admission(
    core, tmp_path, monkeypatch, state
):
    async def scenario():
        async with liability_case(core, tmp_path, monkeypatch) as c:
            # QUEUED is real admission; the other projections model damaged historical state.
            core.store.execute('UPDATE jobs SET state=%s WHERE job_id=%s', (state, c.job_id))
            assert not core.store.rows("SELECT * FROM model_usage")
            value = await unchanged_read(core, c, status=409)
            assert value["error"] == "MEME_RESPONSIBILITY_UNVERIFIED"

    run(scenario())


async def add_reuse(core, c):
    """Original private admission; only source proof changes, cache identity stays fixed."""
    import copy

    payload = copy.deepcopy(c.payload)
    payload["source_evidence"]["usage_basis"] += "; independently retained second source"
    payload["asset"]["source_evidence_sha256"] = digest(payload["source_evidence"])
    response = await c.client.post("/internal/v1/memes/captions", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["job_id"]


@pytest.mark.parametrize("damage", ["none", "root_pointer", "cycle", "missing", "identity",
                                     "limit", "admission", "empty_parent"])
def test_original_fee_and_complete_reuse_ancestry_are_required(core, tmp_path, monkeypatch, damage):
    async def scenario():
        async with liability_case(core, tmp_path, monkeypatch) as c:
            root = c.job_id
            child = await add_reuse(core, c)
            # Admission has really retained a null child UID before root's actual reservation.
            assert await worker(core, [])
            assert core.store.one('SELECT reuse_job_id FROM meme_captions WHERE job_id=%s',
                                  (child,))["reuse_job_id"] == root
            c.job_id, c.path = child, c.path.replace(root, child)
            if damage == "root_pointer":
                core.store.execute('UPDATE meme_captions SET usage_request_id=NULL WHERE job_id=%s',
                                   (root,))
            elif damage == "cycle":
                core.store.execute('UPDATE meme_captions SET reuse_job_id=%s WHERE job_id=%s',
                                   (child, root))
            elif damage in {"missing", "empty_parent"}:
                # Emulate a damaged offline snapshot in the isolated test database.
                core.store.execute("ALTER TABLE meme_captions "
                                   "DROP CONSTRAINT fk_meme_captions_reuse_job_id")
                parent = "job_" + "f" * 32 if damage == "missing" else ""
                core.store.execute("UPDATE meme_captions SET reuse_job_id=%s WHERE job_id=%s",
                                   (parent, root))
                core.store.execute("ALTER TABLE meme_captions ADD CONSTRAINT "
                                   "fk_meme_captions_reuse_job_id FOREIGN KEY (reuse_job_id) "
                                   "REFERENCES meme_captions(job_id) NOT VALID")
            elif damage == "identity":
                core.store.execute('UPDATE meme_captions SET asset_sha256=%s WHERE job_id=%s',
                                   ("f" * 64, root))
            elif damage == "limit":
                monkeypatch.setattr(meme_responsibility, "MAX_ROWS", 1)
            elif damage == "admission":
                core.store.execute('UPDATE model_admissions SET logical_request_id=%s', (child,))
            value = await unchanged_read(core, c, status=200 if damage == "none" else 409)
            if damage == "none":
                assert value["liability_kind"] == "USAGE"
                assert value["usage_owner_job_id"] == root
                assert value["usage"]["state"] == "SETTLED"
                assert core.store.one('SELECT usage_request_id FROM meme_captions WHERE job_id=%s',
                                      (child,))["usage_request_id"] is None

    run(scenario())


def test_terminal_target_cannot_hide_a_nonterminal_no_fee_ancestor(core, tmp_path, monkeypatch):
    async def scenario():
        async with liability_case(core, tmp_path, monkeypatch, daily_calls=0) as c:
            child = await add_reuse(core, c)
            assert await worker(core, [])
            core.store.execute("UPDATE jobs SET state='FAILED' WHERE job_id=%s", (child,))
            core.store.execute("UPDATE jobs SET state='RUNNING' WHERE job_id=%s", (c.job_id,))
            c.path = c.path.replace(c.job_id, child)
            value = await unchanged_read(core, c, status=409)
            assert value["error"] == "MEME_RESPONSIBILITY_UNVERIFIED"

    run(scenario())


@pytest.mark.parametrize("case", ["settings_path", "missing_journal", "authority_ref",
                                  "journal_identity", "model_restore", "cloud_restore"])
def test_original_custody_never_initializes_or_substitutes_evidence(
    core, tmp_path, monkeypatch, case
):
    async def scenario():
        async with liability_case(core, tmp_path, monkeypatch) as c:
            assert await worker(core, [])
            original = Path(core.settings.paid_model.journal_path)
            missing = tmp_path / "must-not-create.jsonl"
            expected = 409
            if case == "settings_path":
                core.settings.paid_model = core.settings.paid_model.model_copy(
                    update={"journal_path": str(missing)})
                expected = 200
            elif case == "missing_journal":
                await asyncio.to_thread(original.rename, tmp_path / "retained-original.jsonl")
            elif case == "authority_ref":
                core.store.execute("UPDATE jobs SET authority_json='{}' WHERE job_id=%s",
                                   (c.job_id,))
            elif case == "journal_identity":
                core.store.execute("UPDATE runtime_meta SET value='unrelated-id' "
                                   "WHERE key='model_budget_journal_id'")
            else:
                core.store.insert("runtime_meta",
                    key=case.replace("_restore", "_budget_restore_required"), value="retained")
                expected = 503
            # Missing-file refusal and substituted settings use logical DB + actual evidence files.
            before = logical_state(core.store), core.ledger.path.read_bytes()
            files = {p: p.read_bytes() for p in tmp_path.rglob("*.jsonl")}
            response = await c.client.post(c.path, json={})
            assert response.status_code == expected, response.text
            assert (logical_state(core.store), core.ledger.path.read_bytes()) == before
            assert {p: p.read_bytes() for p in tmp_path.rglob("*.jsonl")} == files
            assert not missing.exists()
            if "restore" in case:
                assert response.json()["error"] == "OWNER_AUTHORITY_RESTORE_RECONCILIATION_REQUIRED"

    run(scenario())


@pytest.mark.parametrize("case", ["stop", "expiry", "recall", "erasure"])
def test_stopped_expired_recalled_and_erased_fees_remain_original_read_only_metadata(
    core, tmp_path, monkeypatch, case
):
    async def scenario():
        async with liability_case(core, tmp_path, monkeypatch) as c:
            if case == "expiry":
                await renewed_caption(core, c, c.grant.policy.model_copy(
                    update={"expires_at": core.clock.now.fromisoformat(after(core.clock(), 60))}))
            calls = []

            async def interrupt():
                if case == "stop":
                    response = await stop(c.client, c.grant)
                    assert response.status_code == 200, response.text
                elif case == "expiry":
                    core.clock.now = core.clock.now.fromisoformat(after(core.clock(), 172800))
                elif case == "recall":
                    response = await c.client.post("/internal/v1/owner-authority/inputs/recall",
                        json=notice(c.config, c.grant, c.now))
                    assert response.status_code == 200, response.text
                else:
                    with core.store.transaction(control_write=True):
                        core.ledger.expire_events(core.store, [c.event["event_id"]], core.clock())
                raise ValueError("Synthetic loss after original dispatch and interruption")

            assert await worker(core, calls, hook=interrupt) and len(calls) == 1
            original = core.store.one((
                'SELECT state FROM jobs WHERE job_id=%s'
            ), (c.job_id,))["state"]
            value = await unchanged_read(core, c)
            assert value["liability_kind"] == "USAGE" and value["usage"]["state"] == "UNKNOWN"
            assert value["job_state"] == original
            assert value["usage_owner_job_id"] == c.job_id
            if case in {"recall", "erasure"}:
                assert value["body_erased"] and value["job_state"] == "CANCELLED"

    run(scenario())



def test_actual_tcp_read_denies_all_sql_writes_and_caption_or_event_body_reads(
    core, tmp_path, monkeypatch
):
    from contextlib import contextmanager
    from uuid import uuid4

    async def scenario():
        async with liability_case(core, tmp_path, monkeypatch) as c:
            assert await worker(core, [])
            read = meme_responsibility.read
            role = sql.Identifier("caption_reader_" + uuid4().hex)
            db = core.store
            db.execute(sql.SQL("CREATE ROLE {}").format(role))
            try:
                db.execute(
                    sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(role)
                )
                for table, forbidden in {
                    "meme_captions": {"asset_data", "result_json", "context_json", "source_json",
                                      "request_json"},
                    "events": {"body_json"},
                }.items():
                    db.execute(sql.SQL("REVOKE SELECT ON {} FROM {}").format(
                        sql.Identifier(table), role))
                    allowed = [sql.Identifier(row["column_name"]) for row in db.rows(
                        "SELECT column_name FROM information_schema.columns WHERE "
                        "table_schema=current_schema() AND table_name=%s", (table,))
                        if row["column_name"] not in forbidden]
                    db.execute(sql.SQL("GRANT SELECT ({}) ON {} TO {}").format(
                        sql.SQL(",").join(allowed), sql.Identifier(table), role))

                actual_snapshots = []

                def guarded(store, settings, job_id):
                    native_snapshot = store.read_snapshot

                    @contextmanager
                    def observe_snapshot():
                        with native_snapshot():
                            actual_snapshots.append(store.one("SHOW transaction_read_only"))
                            yield

                    store.execute(sql.SQL("SET ROLE {}").format(role))
                    try:
                        store.execute("SET default_transaction_read_only=on")
                        with monkeypatch.context() as reader:
                            reader.setattr(store, "read_snapshot", observe_snapshot)
                            return read(store, settings, job_id)
                    finally:
                        store.execute("RESET default_transaction_read_only")
                        store.execute("RESET ROLE")

                monkeypatch.setattr(meme_responsibility, "read", guarded)
                assert (await unchanged_read(core, c))["usage"]["state"] == "SETTLED"
                assert actual_snapshots == [{"transaction_read_only": "on"}]
            finally:
                db.execute(sql.SQL("DROP OWNED BY {}").format(role))
                db.execute(sql.SQL("DROP ROLE {}").format(role))

    run(scenario())


@pytest.mark.parametrize("body_value", [None, [], {"job_id": "job_" + "f" * 32}, "{}", True])
def test_private_read_requires_an_empty_object_without_echoing_rejected_input(
    core, tmp_path, monkeypatch, body_value
):
    async def scenario():
        async with liability_case(core, tmp_path, monkeypatch) as c:
            before = snapshot(core)
            response = await c.client.post(c.path, json=body_value)
            assert response.status_code == 400 and response.json()["error"] == "INVALID_CONTRACT"
            assert "f" * 32 not in response.text
            assert snapshot(core) == before

    run(scenario())


def test_private_read_requires_bearer_and_a_known_job(core, tmp_path, monkeypatch):
    async def scenario():
        async with liability_case(core, tmp_path, monkeypatch) as c:
            before = snapshot(core)
            unauthenticated = await c.client.post(c.path, json={},
                headers={"Authorization": "Bearer synthetic-wrong"})
            assert unauthenticated.status_code == 401
            missing_body = await c.client.post(c.path)
            assert missing_body.status_code == 400
            unknown = await c.client.post(c.path.replace(c.job_id, "job_" + "f" * 32), json={})
            assert unknown.status_code == 409
            assert snapshot(core) == before

    run(scenario())


async def renewed_caption(core, c, policy):
    """Actual STOP/revision, signed new ingress and original discovery/caption admission."""
    from tests.support.owner_authority import activate

    stopped = await stop(c.client, c.grant)
    assert stopped.status_code == 200, stopped.text
    active, _ = activate(core, policy, revision=2, operation="responsibility-renew-3")
    c.grant = SimpleNamespace(**{**vars(c.grant),
                                 "authority": active["authority"], "policy": policy})
    event, response = await admit(c.client, c.config, c.grant, c.now, 2)
    assert response.status_code == 202, response.text
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s",
                       (response.json()["job_id"],))
    revision = core.store.one('SELECT reply_revision FROM events WHERE event_id=%s',
                              (event["event_id"],))["reply_revision"]
    meme_service.submit(core.store, core.settings, core.clock, meme_service.DiscoveryRequest(
        event_id=event["event_id"], reply_revision=revision,
        intent="celebrate", provider="community"))
    attempt = claim(core)
    reservation(core, attempt)
    result(core, attempt)
    # A different synthetic asset avoids reuse under the stopped first authority.
    c.payload = body(core, event, attempt, c.now, data=synthetic_image())
    c.payload["context"]["reply_revision"] = revision
    response = await c.client.post("/internal/v1/memes/captions", json=c.payload)
    assert response.status_code == 200, response.text
    c.event, c.job_id = event, response.json()["job_id"]
    c.path = "/internal/v1/memes/captions/" + c.job_id + "/responsibility"


def synthetic_image():
    import io

    from PIL import Image

    image = io.BytesIO()
    Image.new("RGB", (8, 6), "blue").save(image, format="PNG")
    return image.getvalue()


def test_legally_redacted_original_policy_is_accounting_evidence_without_reactivation(
    core, tmp_path, monkeypatch
):
    from companion.life_protocol import LifeShadowApproval

    async def scenario():
        async with liability_case(core, tmp_path, monkeypatch) as c:
            original_event = c.event
            scope = LifeShadowApproval(scope="owner_private_life_shadow_v1",
                authorization_sha256="b" * 64, conversation_id=core.store.one(
                    'SELECT conversation_id FROM events WHERE event_id=%s',
                    (c.event["event_id"],))["conversation_id"],
                source_event_ids=[c.event["event_id"]], purposes=["PRIVATE_DIARY"],
                expires_at=after(core.clock(), 3600), max_items=2)
            policy = c.grant.policy.model_copy(update={
                "purposes": [*c.grant.policy.purposes, "life"], "life_shadow": scope})
            await renewed_caption(core, c, policy)
            assert await worker(core, [])
            with core.store.transaction(control_write=True):
                core.ledger.expire_events(core.store, [original_event["event_id"]], core.clock())
            row = core.store.one("SELECT policy_json,policy_redacted,record_sha256 FROM "
                                 "owner_authority_revisions WHERE revision=3")
            assert row["policy_redacted"] == 1 and row["policy_json"] == "{}"
            mode = core.store.one(
                "SELECT value FROM runtime_meta WHERE key='owner_authority_mode'")
            assert mode["value"] == "OFF"
            value = await unchanged_read(core, c)
            assert value["liability_kind"] == "USAGE" and value["usage"]["state"] == "SETTLED"
            assert value["usage_owner_job_id"] == c.job_id and not value["body_erased"]

    run(scenario())



@pytest.mark.parametrize("kind", ["stale_snapshot", "restored_backup"])
def test_old_database_and_actual_backup_cannot_replay_or_clear_restore_barriers(
    core, tmp_path, monkeypatch, kind, empty_database,
):
    import copy
    from contextlib import closing

    import httpx
    from cryptography.fernet import Fernet

    from companion.backup import backup, restore
    from companion.store import Store
    from tests.support.core_http import real_core_http

    async def scenario():
        async with liability_case(core, tmp_path, monkeypatch) as c:
            assert await worker(core, [])
            state = tmp_path / "restored-state"
            key, encrypted = Fernet.generate_key(), tmp_path / "old.backup"
            if kind == "stale_snapshot":
                copy_database(core.settings.database, empty_database, tmp_path)
            else:
                backup(core.settings.database, encrypted, key)
            with core.store.transaction(control_write=True):
                core.ledger.expire_events(core.store, [c.event["event_id"]], core.clock())
            if kind == "restored_backup":
                restore(encrypted, empty_database, key, core.settings.erasure_ledger,
                        state_directory=state)
            with Database(empty_database) as db, closing(Store(db, state)) as stored:
                shadow = copy.copy(core)
                shadow.database = db
                shadow.store = stored
                shadow.settings = core.settings.model_copy(update={"database": empty_database,
                    "state_directory": str(state)})
                if kind == "restored_backup":
                    assert stored.one("SELECT value FROM runtime_meta "
                                      "WHERE key='cloud_budget_restore_required'")
                    assert stored.one("SELECT asset_data FROM meme_captions")["asset_data"] is None
                async with real_core_http(shadow) as port, httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}", trust_env=False,
                    headers={"Authorization": c.client.headers["Authorization"]},
                ) as client:
                    before = snapshot(shadow)
                    response = await client.post(c.path, json={})
                    assert response.status_code == 503, response.text
                    expected = ("ERASURE_REPLAY_REQUIRED" if kind == "stale_snapshot" else
                                "OWNER_AUTHORITY_RESTORE_RECONCILIATION_REQUIRED")
                    assert response.json()["error"] == expected
                    assert snapshot(shadow) == before

    run(scenario())


@pytest.mark.parametrize("changed", ["journal", "ledger"])
def test_original_external_custody_is_pinned_again_before_return(
    core, tmp_path, monkeypatch, changed
):
    from companion import paid_budget

    async def scenario():
        async with liability_case(core, tmp_path, monkeypatch) as c:
            assert await worker(core, [])
            original = paid_budget.journal
            injected = []

            def checked(*args, **kwargs):
                value = original(*args, **kwargs)
                if not injected:
                    path = (Path(core.settings.paid_model.journal_path) if changed == "journal"
                            else core.ledger.path)
                    path.write_bytes(path.read_bytes() + b"\n")
                    injected.append(snapshot(core))
                return value

            monkeypatch.setattr(paid_budget, "journal", checked)
            response = await c.client.post(c.path, json={})
            assert response.status_code == 409, response.text
            assert response.json()["error"] in {
                "MEME_RESPONSIBILITY_UNVERIFIED", "LIVE_TEXT_HANDOFF_LEDGER_CHANGED"}
            assert len(injected) == 1 and snapshot(core) == injected[0]

    run(scenario())


def test_two_actual_fee_owners_in_a_damaged_chain_never_collapse_to_no_admission(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with liability_case(core, tmp_path, monkeypatch) as c:
            assert await worker(core, [])
            response = await c.client.post("/internal/v1/memes/captions",
                json=body(core, c.event, c.attempt, c.now, data=synthetic_image()))
            assert response.status_code == 200, response.text
            other = response.json()["job_id"]
            assert await worker(core, [])
            assert len(core.store.rows("SELECT request_id FROM model_usage")) == 2
            # Both fees are original journaled admissions; only the reuse projection is damaged.
            fields = core.store.one((
                'SELECT asset_sha256,cache_key,identity_json FROM '
                'meme_captions WHERE job_id=%s'
            ), (c.job_id,))
            core.store.execute((
                'UPDATE meme_captions SET reuse_job_id=%s,asset_sha256=%s,cac'
                'he_key=%s,identity_json=%s WHERE job_id=%s'
            ),
                (c.job_id, *fields.values(), other))
            c.path = c.path.replace(c.job_id, other)
            assert (await unchanged_read(core, c, status=409))["error"] == (
                "MEME_RESPONSIBILITY_UNVERIFIED")

    run(scenario())


@pytest.mark.parametrize("state", ["SETTLED", "UNKNOWN", "NO_ADMISSION"])
def test_bounded_three_node_reuse_preserves_the_original_fee_or_complete_no_admission(
    core, tmp_path, monkeypatch, state
):
    async def scenario():
        async with liability_case(core, tmp_path, monkeypatch,
                                  daily_calls=0 if state == "NO_ADMISSION" else 30) as c:
            import copy

            root = c.job_id
            child = await add_reuse(core, c)
            c.payload = copy.deepcopy(c.payload)
            c.payload["source_evidence"]["usage_basis"] += "; third retained source"
            grandchild = await add_reuse(core, c)
            core.store.execute('UPDATE meme_captions SET reuse_job_id=%s WHERE job_id=%s',
                               (child, grandchild))
            calls = []

            async def lost():
                raise ValueError("Synthetic actual dispatch response loss")

            assert await worker(core, calls, hook=lost if state == "UNKNOWN" else None)
            if state == "NO_ADMISSION":
                # The original worker may detach failed predispatch children for later takeover.
                # Retain the complete historical failed chain as a synthetic projection.
                core.store.execute('UPDATE meme_captions SET reuse_job_id=%s WHERE job_id=%s',
                                   (root, child))
                core.store.execute('UPDATE meme_captions SET reuse_job_id=%s WHERE job_id=%s',
                                   (child, grandchild))
                core.store.execute("UPDATE jobs SET state='FAILED' WHERE kind='meme_caption'")
            c.path = c.path.replace(root, grandchild)
            value = await unchanged_read(core, c)
            kind = "NO_ADMISSION" if state == "NO_ADMISSION" else "USAGE"
            assert value["liability_kind"] == kind
            assert value["usage_owner_job_id"] == (None if state == "NO_ADMISSION" else root)
            if state != "NO_ADMISSION":
                assert value["usage"]["state"] == state and len(calls) == 1
            else:
                assert value["usage"] is None and not calls

    run(scenario())



@pytest.mark.parametrize("patch", [
    {"version": True}, {"body_erased": "true"}, {"job_state": "UNKNOWN"},
    {"liability_kind": "NONE"}, {"job_state": "RUNNING"},
    {"usage_owner_job_id": "job_" + "b" * 32}, {"liability_kind": "USAGE"},
    {"result": "Synthetic rejected body"},
])
def test_shared_responsibility_contract_is_strict_and_cannot_invent_terminal_no_admission(patch):
    from pydantic import ValidationError

    from companion_meme_protocol import CaptionResponsibility

    value = {"version": 1, "job_id": "job_" + "a" * 32, "event_id": "evt_" + "e" * 64,
        **{key: "c" * 64 for key in ("request_sha256", "asset_sha256", "source_evidence_sha256",
            "cache_key", "authority_sha256", "lineage_sha256", "erasure_ledger_sha256",
            "budget_journal_id_sha256", "budget_journal_head_sha256")},
        "job_state": "FAILED", "body_erased": True, "liability_kind": "NO_ADMISSION",
        "usage_owner_job_id": None, "usage": None}
    assert CaptionResponsibility.model_validate(value).model_dump(mode="json") == value
    with pytest.raises(ValidationError):
        CaptionResponsibility.model_validate({**value, **patch})



@pytest.mark.parametrize("damage", ["invalid", "conflict"])
def test_unused_preallocated_pointers_still_require_a_consistent_original_chain(
    core, tmp_path, monkeypatch, damage
):
    async def scenario():
        async with liability_case(core, tmp_path, monkeypatch, daily_calls=0) as c:
            root = c.job_id
            child = await add_reuse(core, c)
            assert await worker(core, [])
            assert not core.store.rows("SELECT request_id FROM model_usage")
            assert not core.store.rows("SELECT request_id FROM model_admissions")
            # Explicit damaged terminal projection, retaining the real root's preallocated UID.
            core.store.execute((
                'UPDATE meme_captions SET reuse_job_id=%s,usage_request_id=%s'
                ' WHERE job_id=%s'
            ), (root, "invalid" if damage == "invalid" else "model_" + "f" * 32,
                                  child))
            core.store.execute("UPDATE jobs SET state='FAILED' WHERE job_id=%s", (child,))
            c.path = c.path.replace(root, child)
            assert (await unchanged_read(core, c, status=409))["error"] == (
                "MEME_RESPONSIBILITY_UNVERIFIED")

    run(scenario())

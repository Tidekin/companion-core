"""Actual stopped accounting CLI; original Core worker and private TCP only."""

import asyncio
import json
import os
import shutil
import sqlite3
import sys
from contextlib import asynccontextmanager, closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion_onebot_bridge import meme_accounting_drain as drain
from companion_onebot_bridge.meme_runtime import MemeRuntime
from tests.conftest import run
from tests.test_meme_asset_migration import config_file
from tests.test_meme_core import worker
from tests.test_meme_responsibility import snapshot
from tests.test_meme_responsibility_bridge import bridge_case

ROOT = Path(__file__).resolve().parents[1]


def config_path(c, tmp_path):
    config = c.owner.config
    return config_file(
        tmp_path / "accounting.toml",
        {
            key: getattr(config, key)
            for key in (
                "owner_qq_id",
                "bot_qq_id",
                "bridge_token",
                "onebot_token",
                "protocol_epoch",
                "data_dir",
                "core_url",
                "capture_signing_key",
                "owner_authority",
                "meme_enabled",
            )
        },
    )


async def cli(path, command="owner-meme-accounting-drain"):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-m",
        "companion_onebot_bridge",
        "--config",
        str(path),
        command,
        cwd=ROOT,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(30):
            output, error = await child.communicate()
        assert not error, "Synthetic child stderr must remain empty"
        return child.returncode, json.loads(output)
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


def test_actual_stopped_cli_retains_original_settled_fee_without_content_state_writes(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with bridge_case(core, tmp_path, monkeypatch) as c:
            MemeRuntime(c.owner)  # Original native schema setup, never a drain-side constructor.
            assert await worker(core, [])
            c.owner.stop()
            path = config_path(c, tmp_path)
            database = c.owner.spool.directory / "spool.sqlite3"
            original_journal = c.owner.path.read_bytes()
            c.owner.spool.close()
            with closing(sqlite3.connect(database)) as db:
                old = db.execute(
                    "SELECT state,context_json,outcome_json FROM meme_caption_bindings"
                ).fetchone()
            before = snapshot(core)
            code, value = await cli(path)
            assert code == 0 and value["state"] == "COMPLETE", value
            assert value["attempted"] == value["updated"] == value["settled"] == 1
            with closing(sqlite3.connect(database)) as db:
                current = db.execute(
                    "SELECT state,context_json,outcome_json FROM meme_caption_bindings"
                ).fetchone()
                usage = json.loads(
                    db.execute("SELECT usage_json FROM meme_caption_bindings").fetchone()[0]
                )
            assert old == current and usage["state"] == "SETTLED"
            assert (
                usage["usage_request_id"]
                == core.store.one("SELECT request_id FROM model_usage")["request_id"]
            )
            assert c.owner.path.read_bytes() == original_journal and snapshot(core) == before
            assert c.job_id not in json.dumps(value) and str(tmp_path) not in json.dumps(value)

    run(scenario())


@asynccontextmanager
async def stopped_case(core, tmp_path, monkeypatch, *, state="SETTLED"):
    async with bridge_case(core, tmp_path, monkeypatch) as c:
        MemeRuntime(c.owner)
        calls = []

        async def lost():
            raise ValueError("Synthetic loss after actual model dispatch")

        assert await worker(core, calls, hook=lost if state == "UNKNOWN" else None)
        assert len(calls) == 1
        c.owner.stop()
        c.path = config_path(c, tmp_path)
        c.database = c.owner.spool.directory / "spool.sqlite3"
        c.directory = c.owner.spool.directory
        c.config = c.owner.config
        c.owner.spool.close()
        yield c


@pytest.mark.parametrize("damage", ["missing_job", "duplicate_job", "over_limit", "active"])
def test_invalid_or_incomplete_original_inventory_never_reports_complete(
    core, tmp_path, monkeypatch, damage
):
    async def scenario():
        async with stopped_case(core, tmp_path, monkeypatch) as c:
            with closing(sqlite3.connect(c.database)) as db, db:
                if damage == "missing_job":
                    db.execute("UPDATE meme_caption_bindings SET job_id=NULL")
                elif damage in {"duplicate_job", "over_limit"}:
                    row = list(db.execute("SELECT * FROM meme_caption_bindings").fetchone())
                    row[0] = "f" * 64
                    db.execute("INSERT INTO meme_caption_bindings VALUES (?,?,?,?,?,?,?,?,?)", row)
                else:
                    # Exact original native state cannot be fabricated by changing only this row.
                    db.execute("UPDATE owner_runtime SET state='ACTIVE'")
            if damage == "over_limit":
                monkeypatch.setattr(drain, "MAX_ROWS", 1)
            before = snapshot(core)
            report = await drain.command(c.config, SimpleNamespace())
            assert report["state"] == "UNVERIFIED" and report["unverified"] >= 1
            assert snapshot(core) == before

    run(scenario())


def test_actual_unknown_stays_pending_and_bodyless_clone_does_not_reactivate_content(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with stopped_case(core, tmp_path, monkeypatch, state="UNKNOWN") as c:
            clone_directory = tmp_path / "bodyless-native-clone"
            await asyncio.to_thread(shutil.copytree, c.directory, clone_directory)
            database = clone_directory / "spool.sqlite3"
            with closing(sqlite3.connect(database)) as clone, clone:
                clone.execute(
                    "UPDATE meme_caption_bindings SET context_json=NULL,outcome_json=NULL"
                )
            config = replace(c.config, data_dir=str(clone_directory))
            path = config_path(SimpleNamespace(owner=SimpleNamespace(config=config)), tmp_path)
            before = snapshot(core)
            code, report = await cli(path)
            assert code == 0 and report["state"] == "PENDING", report
            assert report["unknown"] == report["updated"] == 1
            with closing(sqlite3.connect(database)) as db:
                row = db.execute(
                    "SELECT state,context_json,outcome_json,usage_json FROM meme_caption_bindings"
                ).fetchone()
                assert row[:3] == ("QUEUED", None, None)
                assert json.loads(row[3])["state"] == "UNKNOWN"
            assert snapshot(core) == before

    run(scenario())


@asynccontextmanager
async def shared_fee_case(core, tmp_path, monkeypatch):
    async with bridge_case(core, tmp_path, monkeypatch) as c:
        MemeRuntime(c.owner)
        meta = c.catalog.metadata(
            c.catalog.db.execute(
                "SELECT metadata FROM meme_assets WHERE sha=?", (c.sha,)
            ).fetchone()[0]
        )
        c.semantics.rebind_source(
            c.sha,
            replace(meta, usage_basis=meta.usage_basis + "; renewed"),
            check=lambda: c.owner.verify(),
        )
        pending = await c.semantics.advance(
            c.sha, c.adapter, c.context, check=c.check, commit_check=lambda: c.owner.verify()
        )
        assert pending["state"] == "PENDING"
        calls = []
        assert await worker(core, calls) and await worker(core, calls)
        assert len(calls) == 1 and len(core.store.rows("SELECT request_id FROM model_usage")) == 1
        assert c.catalog.db.execute("SELECT COUNT(*) FROM meme_caption_bindings").fetchone()[0] == 2
        c.owner.stop()
        c.path, c.config = config_path(c, tmp_path), c.owner.config
        c.database = c.owner.spool.directory / "spool.sqlite3"
        c.owner.spool.close()
        yield c


def test_failed_early_rows_cannot_starve_later_original_shared_fee(core, tmp_path, monkeypatch):
    from companion_onebot_bridge.network import private_http_client

    async def scenario():
        async with shared_fee_case(core, tmp_path, monkeypatch) as c:
            with closing(sqlite3.connect(c.database)) as db, db:
                original = list(
                    db.execute("SELECT * FROM meme_caption_bindings LIMIT 1").fetchone()
                )
                for i in range(9):
                    row = list(original)
                    row[0], row[5] = f"{i:064x}", "job_" + f"{i:032x}"
                    # Synthetic dangling historical binding: Core genuinely rejects the unknown job.
                    db.execute("INSERT INTO meme_caption_bindings VALUES (?,?,?,?,?,?,?,?,?)", row)
            before = snapshot(core)
            async with private_http_client(c.config.core_url, c.config.bridge_token) as client:
                with drain.open_existing_accounting_store(c.config) as context:
                    assert await drain.drain_pass(context, client) <= 8
                    first = context.index
                    while context.index < len(context.inventory):
                        assert await drain.drain_pass(context, client) > 0
                    report = context.report()
                    assert first > 0 and report["attempted"] == report["inventory"] == 11
                    assert report["unverified"] == 9 and report["settled"] == report["updated"] == 2
                    assert report["remaining"] == 0 and report["state"] == "UNVERIFIED"
            assert len(core.store.rows("SELECT request_id FROM model_usage")) == 1
            assert snapshot(core) == before

    run(scenario())


def test_cancellation_rolls_back_current_projection_and_retains_prior_commit(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with shared_fee_case(core, tmp_path, monkeypatch) as c:
            original, writes = drain.retain_usage_projection, []

            def interrupted(*args):
                original(*args)
                writes.append(1)
                if len(writes) == 2:
                    raise asyncio.CancelledError

            monkeypatch.setattr(drain, "retain_usage_projection", interrupted)
            before = snapshot(core)
            with pytest.raises(asyncio.CancelledError):
                await drain.command(c.config, SimpleNamespace())
            with closing(sqlite3.connect(c.database)) as db:
                rows = db.execute("SELECT state,usage_json FROM meme_caption_bindings").fetchall()
                assert len(rows) == 2 and sum(row[1] is not None for row in rows) == 1
                assert {row[0] for row in rows} == {"QUEUED"}
            assert len(writes) == 2 and snapshot(core) == before

    run(scenario())


@pytest.mark.parametrize("change", ["tuple", "capture", "journal", "inventory"])
def test_actual_response_then_changed_original_custody_cannot_commit_projection(
    core, tmp_path, monkeypatch, change
):
    async def scenario():
        async with stopped_case(core, tmp_path, monkeypatch) as c:
            factory, injected = drain.private_http_client, []

            def checked(*args):
                client = factory(*args)

                async def mutate(response):
                    assert response.status_code == 200
                    if change == "journal":
                        c.owner.path.write_bytes(c.owner.path.read_bytes() + b"\n")
                    else:
                        with closing(sqlite3.connect(c.database)) as db, db:
                            if change == "tuple":
                                db.execute(
                                    "UPDATE meme_caption_bindings SET source_sha=?", ("f" * 64,)
                                )
                            elif change == "capture":
                                db.execute("UPDATE window_inputs SET capture_json='{}'")
                            else:
                                row = list(
                                    db.execute("SELECT * FROM meme_caption_bindings").fetchone()
                                )
                                row[0], row[5] = "f" * 64, None
                                db.execute(
                                    "INSERT INTO meme_caption_bindings VALUES (?,?,?,?,?,?,?,?,?)",
                                    row,
                                )
                    with closing(sqlite3.connect(c.database)) as db:
                        injected.append(tuple(db.iterdump()))

                client.event_hooks["response"].append(mutate)
                return client

            monkeypatch.setattr(drain, "private_http_client", checked)
            before = snapshot(core)
            report = await drain.command(c.config, SimpleNamespace())
            assert report["state"] == "UNVERIFIED" and report["updated"] == 0
            with closing(sqlite3.connect(c.database)) as db:
                assert tuple(db.iterdump()) == injected[0]
            assert snapshot(core) == before

    run(scenario())


def test_sql_surface_is_only_existing_usage_projection_and_original_pending_is_preserved(
    core, tmp_path, monkeypatch
):
    from companion_onebot_bridge.network import private_http_client

    async def scenario():
        async with stopped_case(core, tmp_path, monkeypatch) as c:
            with closing(sqlite3.connect(c.database)) as db, db:
                db.execute(
                    "INSERT INTO meme_runtime_results VALUES (?,?,?,'PENDING')",
                    ("synthetic-retained-attempt", "job_" + "d" * 32, "{}"),
                )
            before = snapshot(core)
            async with private_http_client(c.config.core_url, c.config.bridge_token) as client:
                with drain.open_existing_accounting_store(c.config) as context:
                    statements = []
                    context.db.set_trace_callback(lambda sql: statements.append(sql.split()[0]))
                    await drain.drain_pass(context, client)
                    report = context.report()
                    assert set(statements) == {"BEGIN", "SELECT", "UPDATE", "COMMIT"}
                    assert statements.count("UPDATE") == 1
                    assert report["state"] == "PENDING" and report["result_pending"] == 1
                    assert report["reasons"]["RESULT_PENDING"] == 1
                    # Authorizer also blocks accidental writes beyond the single projection column.
                    with pytest.raises(sqlite3.DatabaseError):
                        context.db.execute("UPDATE meme_caption_bindings SET state='FAILED'")
                    assert (
                        context.db.execute("SELECT state FROM meme_runtime_results").fetchone()[0]
                        == "PENDING"
                    )
            assert snapshot(core) == before

    run(scenario())


@asynccontextmanager
async def native_case(core, tmp_path, monkeypatch, *, cap=50000):
    import base64

    from companion.meme_core import SPEC
    from companion.store import encode
    from companion_meme_protocol import CaptionIdentity
    from companion_meme_sources import Metadata
    from companion_onebot_bridge.event_normalizer import normalize
    from companion_onebot_bridge.meme_semantic import SemanticLibrary
    from companion_onebot_bridge.meme_semantic_http import CaptionHTTP, SemanticHTTP
    from companion_onebot_bridge.network import private_http_client
    from companion_onebot_bridge.owner_runtime import OwnerRuntime
    from companion_onebot_bridge.spool import Spool
    from tests.support.onebot_events import raw_event
    from tests.test_meme_responsibility import liability_case

    async with liability_case(core, tmp_path, monkeypatch, cap=cap) as c:
        config = replace(
            c.config,
            data_dir=str(tmp_path / "caption-native"),
            core_url=str(c.client.base_url).rstrip("/"),
            meme_enabled=True,
            capture_signing_key=c.grant.key.private_bytes_raw().hex(),
            owner_authority=c.grant.authority,
        )
        with Spool(config) as spool:
            owner = OwnerRuntime(spool, clock=lambda: c.now)
            raw = json.dumps(raw_event(1, text="合成主人输入1", time=int(c.now.timestamp())))
            event = normalize(raw, config, now=c.now.timestamp())
            assert owner.persist_capture(event, raw, received_at=c.now, peer=("127.0.0.1", 3001))
            runtime = MemeRuntime(owner)
            proof = c.payload["source_evidence"]
            meta = Metadata(
                "like",
                "Synthetic hint",
                (),
                (),
                (),
                **{
                    k: proof[k]
                    for k in (
                        "source_page",
                        "asset_url",
                        "license",
                        "usage_basis",
                        "fetched_at",
                        "revision",
                    )
                },
                acquisition_json=encode(proof["acquisition"]),
            )
            sha = runtime.catalog.add(
                base64.b64decode(c.payload["asset"]["data_base64"]),
                meta,
                check=lambda: owner.verify(),
            )
            semantics = SemanticLibrary(
                runtime.catalog, CaptionIdentity(**c.payload["identity"]), SPEC
            )

            async def check():
                owner.verify()

            async with private_http_client(config.core_url, config.bridge_token) as client:
                adapter = CaptionHTTP(
                    SemanticHTTP(owner, client, check=lambda: owner.verify()), runtime.catalog
                )
                assert (
                    await semantics.advance(
                        sha,
                        adapter,
                        c.payload["context"],
                        check=check,
                        commit_check=lambda: owner.verify(),
                    )
                )["state"] == "PENDING"
                c.owner, c.config, c.semantics, c.runtime = owner, config, semantics, runtime
                c.path = config_path(c, tmp_path)
                c.database, c.directory = spool.directory / "spool.sqlite3", spool.directory
                yield c


def test_real_no_admission_ignores_preallocated_uid_without_persisting_absence(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with native_case(core, tmp_path, monkeypatch, cap=1) as c:
            calls = []
            assert await worker(core, calls) and not calls
            assert not core.store.rows("SELECT request_id FROM model_usage")
            c.owner.stop()
            c.owner.spool.close()
            with closing(sqlite3.connect(c.database)) as db:
                original = tuple(db.iterdump())
            before = snapshot(core)
            report = await drain.command(c.config, SimpleNamespace())
            assert report["state"] == "COMPLETE" and report["no_admission"] == 1
            assert report["updated"] == report["unverified"] == 0
            with closing(sqlite3.connect(c.database)) as db:
                assert tuple(db.iterdump()) == original
            assert snapshot(core) == before

    run(scenario())


def test_actual_reserved_admission_stays_pending_without_finishing_the_worker(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with native_case(core, tmp_path, monkeypatch) as c:
            entered, release = asyncio.Event(), asyncio.Event()

            async def held():
                entered.set()
                await release.wait()

            calls = []
            task = asyncio.create_task(worker(core, calls, hook=held))
            try:
                async with asyncio.timeout(5):
                    await entered.wait()
                c.owner.stop()
                c.owner.spool.close()
                before = snapshot(core)
                report = await drain.command(c.config, SimpleNamespace())
                assert report["state"] == "PENDING" and report["reserved"] == 1
                assert not task.done() and len(calls) == 1 and snapshot(core) == before
            finally:
                release.set()
                assert await task

    run(scenario())


def test_legitimate_local_success_survives_raw_core_erasure_cancellation(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with bridge_case(core, tmp_path, monkeypatch) as c:
            MemeRuntime(c.owner)
            assert await worker(core, [])
            assert (
                await c.semantics.advance(
                    c.sha,
                    c.adapter,
                    c.context,
                    check=c.check,
                    commit_check=lambda: c.owner.verify(),
                )
            )["state"] == "READY"
            with core.store.transaction():
                core.ledger.expire_events(core.store, [c.context["event_id"]], core.clock())
            c.owner.stop()
            database, config = c.owner.spool.directory / "spool.sqlite3", c.owner.config
            c.owner.spool.close()
            with closing(sqlite3.connect(database)) as db:
                old = tuple(db.iterdump())
            before = snapshot(core)
            report = await drain.command(config, SimpleNamespace())
            assert report["state"] == "COMPLETE" and report["cancelled"] == 1
            assert report["settled"] == 1 and report["updated"] == 0
            with closing(sqlite3.connect(database)) as db:
                assert tuple(db.iterdump()) == old
                assert db.execute("SELECT state FROM meme_caption_bindings").fetchone()[0] == (
                    "SUCCEEDED"
                )
            assert snapshot(core) == before

    run(scenario())


@pytest.mark.parametrize("sidecar", ["-wal", "-journal", "-shm"])
def test_nonempty_recovery_sidecar_is_preserved_without_opening_or_repair(
    core, tmp_path, monkeypatch, sidecar
):
    async def scenario():
        async with stopped_case(core, tmp_path, monkeypatch) as c:
            original = c.database.read_bytes()
            path = Path(str(c.database) + sidecar)
            await asyncio.to_thread(path.write_bytes, b"SYNTHETIC_RECOVERY_REQUIRED")
            before = snapshot(core)
            report = await drain.command(c.config, SimpleNamespace())
            assert report["state"] == "UNVERIFIED" and report["attempted"] == 0
            assert c.database.read_bytes() == original
            assert await asyncio.to_thread(path.read_bytes) == b"SYNTHETIC_RECOVERY_REQUIRED"
            assert snapshot(core) == before

    run(scenario())


def test_actual_native_host_stop_exit_restart_gate_then_explicit_cli_is_the_reachable_boundary(
    core, tmp_path, monkeypatch
):
    import base64

    import httpx
    from websockets.asyncio.server import serve

    from companion import meme_service
    from companion.meme_core import SPEC
    from companion.store import encode
    from companion_meme_protocol import CaptionIdentity
    from companion_meme_sources import Metadata
    from companion_onebot_bridge.meme_semantic import SemanticLibrary
    from companion_onebot_bridge.meme_semantic_http import CaptionHTTP, SemanticHTTP
    from tests.native_continuous_support import until
    from tests.support.native_meme import NativeCase, capture
    from tests.support.onebot import onebot_http
    from tests.test_meme_core import body
    from tests.test_meme_service import claim, reservation, result, submit

    async def spawn(path, operation, log):
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            "-m",
            "tests.meme_accounting_host_process",
            str(path),
            operation,
            str(log),
            cwd=ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def finish(child):
        async with asyncio.timeout(35):
            out, error = await child.communicate()
        assert not error
        return child.returncode, json.loads(out.decode().strip().splitlines()[-1])

    async def scenario():
        case = NativeCase(core, tmp_path, rules=(), semantic=True, discovery=True)
        runtime = MemeRuntime(case.owner)
        event = capture(runtime, number=1, text="普通合成文本")
        children, held, connected, sends = [], asyncio.Event(), asyncio.Event(), []

        async def socket(connection):
            connected.set()
            await held.wait()

        async with case.client(runtime) as client:
            assert await case.owner.flush_one(client)
            # Foreground availability is the original documented unit precondition.
            core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE kind='chat'")
            submit(core, event)
            attempt = claim(core)
            reservation(core, attempt)
            result(core, attempt)
            payload = body(core, event, attempt, core.clock.now)
            proof = payload["source_evidence"]
            meta = Metadata(
                "like",
                "Synthetic hint",
                (),
                (),
                (),
                **{
                    k: proof[k]
                    for k in (
                        "source_page",
                        "asset_url",
                        "license",
                        "usage_basis",
                        "fetched_at",
                        "revision",
                    )
                },
                acquisition_json=encode(proof["acquisition"]),
            )
            sha = runtime.catalog.add(
                base64.b64decode(payload["asset"]["data_base64"]),
                meta,
                check=lambda: case.owner.verify(),
            )
            semantics = SemanticLibrary(
                runtime.catalog, CaptionIdentity(**payload["identity"]), SPEC
            )

            async def check():
                case.owner.verify()

            adapter = CaptionHTTP(
                SemanticHTTP(case.owner, client, check=lambda: case.owner.verify()), runtime.catalog
            )
            assert (
                await semantics.advance(
                    sha,
                    adapter,
                    payload["context"],
                    check=check,
                    commit_check=lambda: case.owner.verify(),
                )
            )["state"] == "PENDING"
            assert await worker(core, [])

            async def response_lost(response):
                if response.request.url.path.startswith("/internal/v1/memes/captions/"):
                    raise httpx.ReadError("Synthetic late receipt response loss")

            client.event_hooks["response"].append(response_lost)
            with pytest.raises(httpx.ReadError):
                await semantics.advance(
                    sha,
                    adapter,
                    payload["context"],
                    check=check,
                    commit_check=lambda: case.owner.verify(),
                )
            client.event_hooks["response"].remove(response_lost)
            assert case.spool.db.execute("SELECT state FROM meme_caption_bindings").fetchone()[
                0
            ] == ("UNKNOWN")
            assert semantics._status(sha)["state"] == "QUARANTINED"
            # Retain the original query refusal; the unit foreground has no query receipt.
            with pytest.raises(httpx.HTTPStatusError):
                await runtime.semantic_offer(client, event["event_id"], reply_revision=1)
            assert (
                case.spool.db.execute("SELECT state FROM meme_runtime_semantic_offers").fetchone()[
                    0
                ]
                == "DEFERRED"
            )
            from companion import paid_budget

            with core.store.transaction():
                assert not paid_budget.journal(
                    core.store,
                    core.settings.paid_model,
                    record=None,
                    initialize=False,
                    allow_settlement=True,
                )["blocked_request_ids"]
            idle = []
            original = meme_service.lease

            def observed(*args, **kwargs):
                value = original(*args, **kwargs)
                if value["item"] is None:
                    idle.append(1)
                return value

            monkeypatch.setattr(meme_service, "lease", observed)
            case.spool.close()
            with onebot_http(sends, case.owner.config.onebot_token) as port:
                async with serve(socket, "127.0.0.1", 0) as websocket:
                    config = replace(
                        case.owner.config,
                        onebot_http_url=f"http://127.0.0.1:{port}",
                        onebot_ws_url=f"ws://127.0.0.1:{websocket.sockets[0].getsockname()[1]}",
                        delivery_enabled=False,
                        poll_seconds=0.05,
                    )
                    path = config_file(
                        tmp_path / "daemon.toml",
                        {
                            key: getattr(config, key)
                            for key in (
                                "owner_qq_id",
                                "bot_qq_id",
                                "bridge_token",
                                "onebot_token",
                                "protocol_epoch",
                                "data_dir",
                                "core_url",
                                "capture_signing_key",
                                "owner_authority",
                                "meme_enabled",
                                "meme_semantics_enabled",
                                "meme_discovery_enabled",
                                "onebot_http_url",
                                "onebot_ws_url",
                                "delivery_enabled",
                                "poll_seconds",
                            )
                        },
                    )
                    daemon_log = tmp_path / "daemon-io.json"
                    daemon = await spawn(path, "daemon", daemon_log)
                    children.append(daemon)
                    try:
                        await until(lambda: len(idle) >= 2, deadline_seconds=35)
                        assert connected.is_set() and daemon.returncode is None
                        blocked_code, blocked = await cli(path)
                        assert blocked_code == 0 and blocked["state"] == "UNVERIFIED"
                        _, stopped = await cli(path, "owner-stop")
                        # The daemon may release its lock before this command opens Spool.
                        assert stopped["state"] in {"STOP_REQUESTED", "STOPPED"}
                        assert stopped["authority"] == case.owner.authority
                        assert stopped["core_stop_confirmed"] is False
                        assert case.owner.stop_path.read_bytes() == b"OWNER_STOP\n"
                        assert (await finish(daemon))[0] == 1
                        _, completed = await cli(path, "owner-stop")
                        assert completed["state"] == "STOPPED"
                        restart_log = tmp_path / "restart-io.json"
                        restart = await spawn(path, "daemon", restart_log)
                        children.append(restart)
                        assert (await finish(restart))[0] == 1
                        assert json.loads(restart_log.read_text("utf-8")) == []
                        before = snapshot(core)
                        drain_log = tmp_path / "drain-io.json"
                        accounting = await spawn(path, "owner-meme-accounting-drain", drain_log)
                        children.append(accounting)
                        code, report = await finish(accounting)
                        assert code == 0 and report["state"] == "COMPLETE", report
                        assert report["settled"] == report["updated"] == 1
                        paths = json.loads(drain_log.read_text("utf-8"))
                        assert len(paths) == 1 and paths[0].endswith("/responsibility")
                        assert snapshot(core) == before and not sends
                        assert "EXTERNAL_REFUSED" not in daemon_log.read_text("utf-8")
                    finally:
                        held.set()
                        for child in children:
                            if child.returncode is None:
                                child.kill()
                                await child.wait()

    run(scenario())


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
def test_real_http_hook_timeout_advances_but_caller_cancellation_propagates(
    core, tmp_path, monkeypatch, mode
):
    async def scenario():
        async with shared_fee_case(core, tmp_path, monkeypatch) as c:
            with closing(sqlite3.connect(c.database)) as db:
                first = db.execute(
                    "SELECT job_id FROM meme_caption_bindings ORDER BY request_sha LIMIT 1"
                ).fetchone()[0]
                original = tuple(db.iterdump())
            factory, entered, cancelled = drain.private_http_client, asyncio.Event(), []

            def delayed(*args):
                client = factory(*args)

                async def before(request):
                    if first in request.url.path:
                        entered.set()
                        try:
                            await asyncio.Event().wait()
                        except asyncio.CancelledError:
                            cancelled.append(1)
                            raise

                client.event_hooks["request"].append(before)
                return client

            monkeypatch.setattr(drain, "private_http_client", delayed)
            before = snapshot(core)
            task = asyncio.create_task(drain.command(c.config, SimpleNamespace()))
            async with asyncio.timeout(5):
                await entered.wait()
            if mode == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                with closing(sqlite3.connect(c.database)) as db:
                    assert tuple(db.iterdump()) == original
                # Actual original OS lock is released on cancellation.
                with drain.open_existing_accounting_store(c.config):
                    pass
            else:
                report = await task
                assert report["attempted"] == 2 and report["remaining"] == 0
                assert report["unverified"] == 1 and report["settled"] == report["updated"] == 1
                assert report["state"] == "UNVERIFIED"
            assert cancelled == [1] and snapshot(core) == before

    run(scenario())

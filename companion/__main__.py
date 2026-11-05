import argparse
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import random
import sqlite3
import sys
from contextlib import ExitStack, closing
from pathlib import Path

import httpx
import uvicorn
from pydantic import ValidationError

from companion import runtime_config, shutdown
from companion.backup import backup, restore
from companion.config import Settings
from companion.contracts import CoreError
from companion.model import ModelClient
from companion.runtime import Runtime
from companion.store import Store, encode, migrate, uid, utcnow
from companion.worker_lock import single_worker
from companion.worker_loop import Recorder
from companion.worker_schedule import ClaimBusy, run_native_unit
from companion.worker_wakeup import Maintenance, Wakeup


async def worker(settings):
    from companion.core import Core

    async with httpx.AsyncClient(timeout=settings.timeout_seconds, trust_env=False) as client:
        core = Core(settings, ModelClient(settings, client))
        worker_id = uid("worker")
        core.store.configuration_worker = "worker"
        loop = Recorder(core.store)
        try:
            maintenance_clock = Maintenance()
            with Wakeup(core.store, settings, "worker", core.clock) as wake:
                # A stop request is honoured only here, between units.
                while not shutdown.requested():
                    if not await runtime_config.worker_boundary(core.store, settings, core.clock):
                        break
                    scan = wake.before_scan()
                    try:
                        result = await run_native_unit(core.run_once, worker_id)
                    except ClaimBusy:
                        # Yield this scan only. Never replay a business unit that
                        # already dispatched a model request or QQ operation.
                        await asyncio.sleep(random.uniform(0.05, 0.15))
                        continue
                    maintenance = not result.progressed and maintenance_clock.due()
                    if maintenance:
                        try:
                            core.expire_raw_events()
                            core.cleanup_media()
                        except sqlite3.OperationalError as error:
                            if (getattr(error, "sqlite_errorcode", 0) & 0xFF != sqlite3.SQLITE_BUSY
                                or core.store.db.in_transaction):
                                raise
                            # This is native local maintenance, without model or
                            # delivery dispatch. Its own durable recovery remains authoritative.
                            await asyncio.sleep(random.uniform(0.05, 0.15))
                            continue
                        maintenance_clock.completed()
                    loop.completed(maintenance=maintenance,
                                   waiting=not result.progressed and not maintenance)
                    if not result.progressed and not maintenance:
                        await wake.wait(scan, result, maintenance_clock.next_at)
        finally:
            core.close()


def trusted_proxy_ips(value):
    """Trust only explicit proxy socket addresses, never ambient forwarding settings."""
    try:
        return [str(ipaddress.ip_address(item.strip())) for item in value.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("Use fixed proxy IP addresses, comma separated") from None


def main():
    parser = argparse.ArgumentParser(description="Companion Core v0.5")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("worker")
    sub.add_parser("life-worker")
    sub.add_parser("executive-worker")
    sub.add_parser("content-worker")
    diagnostic = sub.add_parser("status")
    diagnostic.add_argument("--integrity", action="store_true")
    inspector = sub.add_parser("inspect-context")
    inspector.add_argument("--trace-id", required=True)
    observation = sub.add_parser("observe")
    observation.add_argument("--output", required=True)
    observation.add_argument("--samples", type=int, default=3)
    observation.add_argument("--interval-seconds", type=float, default=5)
    observation.add_argument("--max-gap-seconds", type=float)
    observation.add_argument("--url")
    observation.add_argument("--allow-external", action="store_true")
    observation.add_argument("--evidence-type", choices=("local_synthetic", "authorized_runtime"),
                             default="local_synthetic")
    observation.add_argument("--authorization-sha256")
    server = sub.add_parser("serve")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--forwarded-allow-ips", type=trusted_proxy_ips, default=[],
                        help="Trust forwarding headers only from these fixed proxy IPs")
    server.add_argument("--memory-control-grant")
    server.add_argument("--memory-control-sha256")
    archive = sub.add_parser("backup")
    archive.add_argument("destination")
    archive.add_argument("--immutable-source", action="store_true",
                         help="Assert all source writers and maintenance stay stopped before "
                              "invocation through completion; reject WAL/SHM/journal sidecars")
    recovery = sub.add_parser("restore")
    recovery.add_argument("source")
    recovery.add_argument("destination")
    correction = sub.add_parser("reconcile-video-rates")
    correction.add_argument("--request", required=True)
    correction.add_argument("--request-sha256", required=True)
    for name in ("preview-model-accounting", "record-model-accounting"):
        accounting = sub.add_parser(name)
        accounting.add_argument("--request", required=True)
        accounting.add_argument("--request-sha256", required=True)
        accounting.add_argument("--evidence", required=True)
    for name in ("preview-model-carry", "carry-closed-model-attempts"):
        carry = sub.add_parser(name)
        carry.add_argument("--request", required=True)
        carry.add_argument("--request-sha256", required=True)
    unstarted = sub.add_parser("close-unstarted-model-attempt")
    unstarted.add_argument("--request-id", required=True)
    unstarted.add_argument("--commit", action="store_true",
                           help="Append the closure; without it only the record is previewed")
    args = parser.parse_args()
    try:
        settings = (Settings.load(apply_operator_config=False)
                    if args.command in {"backup", "restore"} else Settings.load())
        if args.command == "init":
            migrate(settings.database)
            with Runtime(settings):
                pass
            print("Database migrated; identity and erasure ledger initialized.")
        elif args.command == "serve":
            from companion.api import create_app
            from companion.memory_control import load_grant

            if bool(args.memory_control_grant) != bool(args.memory_control_sha256):
                raise ValueError("MEMORY_CONTROL_APPROVAL_REQUIRED")
            grant = (load_grant(args.memory_control_grant, args.memory_control_sha256)
                     if args.memory_control_grant else None)
            uvicorn.run(
                create_app(settings, memory_control_grant=grant,
                           memory_control_sha256=args.memory_control_sha256),
                host=args.host,
                port=args.port,
                access_log=False,
                proxy_headers=bool(args.forwarded_allow_ips),
                forwarded_allow_ips=args.forwarded_allow_ips,
            )
        elif args.command == "worker":
            logging.getLogger("httpx").setLevel(logging.WARNING)
            with single_worker(settings.database):
                shutdown.install()
                asyncio.run(worker(settings))
        elif args.command == "status":
            from companion.ops_status import snapshot

            print(encode(snapshot(settings, integrity=args.integrity)))
        elif args.command == "inspect-context":
            from companion.beta_observation import inspect_context

            print(encode(inspect_context(settings, args.trace_id)))
        elif args.command == "observe":
            from companion.observe import http_collector, observe
            from companion.ops_status import snapshot

            if args.allow_external and (not args.url or args.evidence_type != "authorized_runtime"):
                raise ValueError("OBSERVATION_EXTERNAL_AUTHORIZATION_REQUIRED")
            collect = (http_collector(settings, args.url, allow_external=args.allow_external,
                                      authorization_sha256=args.authorization_sha256)
                       if args.url else lambda: (snapshot(settings), None))
            print(encode(observe(
                collect, args.output, samples=args.samples, interval_seconds=args.interval_seconds,
                max_gap_seconds=args.max_gap_seconds, evidence_type=args.evidence_type,
                authorization_sha256=args.authorization_sha256,
                source="http_endpoint" if args.url else "local_database",
            )))
        elif args.command == "life-worker":
            from companion.life_worker import life_worker

            logging.getLogger("httpx").setLevel(logging.WARNING)
            # Life now owns diary generation. Also hold the retired diary-worker lock so
            # an orphaned pre-upgrade diary container fails this start loudly instead of
            # writing diaries in parallel.
            with (single_worker(settings.database, "life-worker"),
                  single_worker(settings.database, "diary-worker")):
                shutdown.install()
                asyncio.run(life_worker(settings))
        elif args.command == "executive-worker":
            from companion.executive_worker import executive_worker

            logging.getLogger("httpx").setLevel(logging.WARNING)
            with single_worker(settings.database, "executive-worker"):
                shutdown.install()
                asyncio.run(executive_worker(settings))
        elif args.command == "backup":
            backup(settings.database, args.destination, os.environ["COMPANION_BACKUP_KEY"].encode(),
                   immutable_source=args.immutable_source)
            print("Encrypted backup saved. Keep the latest erasure ledger separately.")
        elif args.command == "content-worker":
            from companion.content_worker import content_worker

            logging.getLogger("httpx").setLevel(logging.WARNING)
            with single_worker(settings.database, "content-worker"):
                shutdown.install()
                asyncio.run(content_worker(settings))
        elif args.command == "restore":
            restore(
                args.source,
                args.destination,
                os.environ["COMPANION_BACKUP_KEY"].encode(),
                settings.erasure_ledger,
            )
            print("Restored to a new database; erasure ledger replay complete.")
        elif args.command in {"preview-model-carry", "carry-closed-model-attempts"}:
            from companion import model_carry, owner_authority
            from companion.model_carry_controller import verify
            from companion_delivery_protocol import digest

            raw = Path(args.request).read_bytes()
            if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != args.request_sha256:
                raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409)
            request = json.loads(raw)
            with ExitStack() as locks:
                for kind in ("worker", "diary-worker", "life-worker", "executive-worker",
                             "content-worker"):
                    locks.enter_context(single_worker(settings.database, kind))
                verified = verify(request["executors"])
                with Runtime(settings) as runtime:
                    if args.command == "preview-model-carry":
                        with runtime.store.transaction():
                            view = owner_authority.custody(runtime.store, settings, settlement=True)
                            value = model_carry.manifest(runtime.store, settings,
                                request["authority"], request["successor_window_id"],
                                verified.value, view)
                        print(encode({"manifest": value, "manifest_sha256": digest(value)}))
                    else:
                        print(encode(model_carry.prepare(runtime.store, settings, request,
                                                       verified, utcnow(),
                                                       request_sha256=args.request_sha256)))
        elif args.command == "close-unstarted-model-attempt":
            from companion import model_carry

            with ExitStack() as locks:
                for kind in ("worker", "diary-worker", "life-worker", "executive-worker",
                             "content-worker"):
                    locks.enter_context(single_worker(settings.database, kind))
                with Runtime(settings) as runtime:
                    print(encode(model_carry.close_unstarted(
                        runtime.store, settings, args.request_id, utcnow(),
                        commit=args.commit)))
        elif args.command in {"preview-model-accounting", "record-model-accounting"}:
            from companion import model_accounting

            request = model_accounting.load_request(args.request, args.request_sha256)
            action = (model_accounting.preview if args.command == "preview-model-accounting"
                      else model_accounting.record)
            with closing(Store(settings.database)) as store:
                print(encode(action(store, settings, request, args.evidence)))
        elif args.command == "reconcile-video-rates":
            from companion.video_accounting import reconcile

            raw = Path(args.request).read_bytes()
            if len(raw) > 4096 or hashlib.sha256(raw).hexdigest() != args.request_sha256:
                raise CoreError("VIDEO_RATE_CORRECTION_INVALID", 400)
            with ExitStack() as locks:
                for kind in (
                    "worker", "diary-worker", "life-worker", "executive-worker", "content-worker",
                ):
                    locks.enter_context(single_worker(settings.database, kind))
                with Runtime(settings) as runtime:
                    print(encode(reconcile(runtime.store, settings, json.loads(raw))))
    except ValidationError as error:
        # Do not print Pydantic input values, which may contain secrets.
        print(
            "Invalid configuration: "
            + ", ".join(
                ".".join(map(str, e["loc"])) + " " + e["msg"]
                for e in error.errors(include_input=False)
            ),
            file=sys.stderr,
        )
        sys.exit(2)
    except CoreError as error:
        print(error.code, file=sys.stderr)
        sys.exit(2)
    except KeyError:
        print("Required environment variable missing.", file=sys.stderr)
        sys.exit(2)
    except (OSError, ValueError):
        print("Command failed; no diagnostic details or private paths emitted.", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("Interrupted; an observation file without an end record is incomplete.",
              file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()

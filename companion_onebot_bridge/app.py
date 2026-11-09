import asyncio
import hashlib
import json
import os
import signal
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .config import BridgeConfig
from .event_normalizer import decode, normalize, redact
from .metrics import Metrics, emit
from .network import private_http_client, websocket
from .onebot_client import OneBotClient
from .outbound import deliver_tick
from .reference_reader import enrich
from .spool import Spool, encode

# A stop signal lets every loop finish its current unit (one send, one capture, one media
# transfer); only a unit still running after this long is cancelled. It exceeds one bounded
# send plus its result report; whoever stops the container must wait longer than this.
SHUTDOWN_GRACE_SECONDS = 60


@asynccontextmanager
async def close_on_stop(stop, ws):
    """Close the socket on stop, which ends the frame loop after the frame being persisted."""
    async def close():
        await stop.wait()
        await ws.close()

    task = asyncio.create_task(close())
    try:
        yield ws
    finally:
        task.cancel()


async def drain(tasks):
    """After stop, wait for each loop's current unit; cancel only what outlives the grace."""
    _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_SECONDS)
    for task in pending:
        task.cancel()
    emit("bridge_shutdown", status="CANCELLED" if pending else "CLEAN")
    return not pending


async def pause(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except TimeoutError:
        pass


async def receive(config, spool, wake, stop, metrics, onebot=None, *, runtime=None, core=None):
    if runtime is None and spool.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='test_window'").fetchone():
        raise ValueError("CONTROLLED_WINDOW_REQUIRES_ONCE_COMMANDS")
    delay = 0.25
    while not stop.is_set():
        try:
            async with (await websocket(config, check=runtime.verify if runtime else None) as ws,
                        close_on_stop(stop, ws)):
                metrics.emit("ws_connected")
                if runtime is not None and onebot is not None and config.history_backfill:
                    from .backfill import backfill

                    # The socket was closed until now; read back what the owner sent.
                    await backfill(runtime, core, onebot, wake)
                async for frame in ws:
                    received_ns = time.perf_counter_ns()
                    received_at = datetime.now(UTC)
                    try:
                        if runtime is not None:
                            from . import owner_notices, owner_pokes

                            if owner_notices.put(runtime, frame, received_at, ws.remote_address):
                                wake.set()
                                continue
                            try:
                                poked = await owner_pokes.put(runtime, core, frame,
                                                              received_at, ws.remote_address)
                            except httpx.HTTPError as error:
                                from .held_inputs import transient

                                if not transient(error):
                                    raise
                                # Pokes are notices without history; one lost here is logged.
                                emit("owner_poke", status="CORE_UNAVAILABLE")
                                continue
                            if poked:
                                wake.set()
                                continue
                        payload = normalize(frame, config)
                        raw = (decode(frame, config.max_frame_bytes, max_depth=(
                            20 if config.rich_input_scope == "references_v1" else 8
                        )) if payload else None)
                    except (ValueError, UnicodeError):
                        metrics.emit("event_rejected")
                        continue
                    if payload:
                        if runtime is not None:
                            from . import held_inputs
                            from .capture import prepare_owner_input

                            # Behind a held frame, or while Core is unreachable, keep the frame
                            # unverified; upload admits it once Core answers again.
                            if held_inputs.waiting(runtime):
                                if held_inputs.hold(runtime, frame, received_at,
                                                    ws.remote_address):
                                    wake.set()
                                continue
                            try:
                                prepared = await prepare_owner_input(
                                    runtime, core, onebot, frame, received_at, ws.remote_address)
                            except httpx.HTTPError as error:
                                if not held_inputs.transient(error):
                                    raise
                                metrics.emit("core_unavailable")
                                if held_inputs.hold(runtime, frame, received_at,
                                                    ws.remote_address):
                                    wake.set()
                                continue
                            if prepared is None:
                                continue
                            payload = prepared["event"]
                            added = runtime.persist_capture(payload, frame,
                                received_at=received_at, peer=ws.remote_address,
                                media=prepared["media"])
                            if added:
                                metrics.start(payload, received_ns)
                                wake.set()
                            else:
                                metrics.emit("duplicate_suppressed")
                            continue
                        fingerprint = hashlib.sha256(
                            encode(
                                {
                                    "message": raw["message"],
                                    "time": raw["time"],
                                }
                            ).encode()
                        ).hexdigest()
                        prior = spool.db.execute(
                            "SELECT fingerprint FROM inbox WHERE key=?", (payload["event_id"],),
                        ).fetchone()
                        if prior:
                            if prior[0] != fingerprint:
                                raise ValueError("SOURCE_EVENT_CONFLICT")
                            metrics.emit("duplicate_suppressed")
                            continue
                        if payload["payload"].get("rich"):
                            if onebot is None:
                                async with private_http_client(
                                    config.onebot_http_url, config.onebot_token,
                                ) as protocol:
                                    await enrich(payload, raw["message"], OneBotClient(
                                        config, protocol), lambda text: redact(text, config))
                            else:
                                await enrich(payload, raw["message"], onebot,
                                             lambda text: redact(text, config))
                        # Stop on storage/capacity/conflict errors; never silently lose the frame.
                        added = spool.put(payload, fingerprint)
                        if added:
                            metrics.start(payload, received_ns)
                            metrics.emit(
                                "t_spool_commit",
                                payload["trace_id"],
                                elapsed_ms=(time.perf_counter_ns() - received_ns) / 1e6,
                            )
                            wake.set()
                            delay = 0.25
                        else:
                            metrics.emit("duplicate_suppressed")
            metrics.emit("ws_disconnected")
        except InvalidStatus as error:
            if error.response.status_code in {401, 403}:
                raise ValueError("ONEBOT_AUTH_REJECTED") from None
            metrics.emit("ws_disconnected")
        except (OSError, TimeoutError, ConnectionClosed):
            metrics.emit("ws_disconnected")
        await pause(stop, delay)
        delay = min(delay * 2, 30)


async def upload(client, spool, wake, stop, metrics, *, runtime=None, memes=None, onebot=None):
    while not stop.is_set():
        wake.clear()
        try:
            if runtime is not None and onebot is not None:
                from .held_inputs import promote_one

                while not stop.is_set() and await promote_one(runtime, client, onebot):
                    pass
            flush = runtime.flush_one if runtime is not None else spool.flush_one
            while payload := await flush(client):
                metrics.accepted(payload)
                if memes is not None:
                    memes.admitted(payload["event_id"], payload=payload)
        except httpx.TransportError:
            metrics.emit("core_unavailable")
            await pause(stop, 1)
            continue
        except httpx.HTTPStatusError as error:
            if error.response.status_code < 500:
                raise ValueError("CORE_INBOUND_REJECTED") from None
            metrics.emit("core_unavailable")
            await pause(stop, 1)
            continue
        try:
            await asyncio.wait_for(wake.wait(), 1)
        except TimeoutError:
            pass


async def deliver(client, onebot, spool, stop, metrics, *, runtime=None, protocol=None):
    while not stop.is_set():
        try:
            guard = None
            if runtime is not None:
                from .owner_send import OwnerOneBot, OwnerSendGuard

                # Reports remain replayable after STOP; sends never are.
                if await spool.flush_result(client, runtime=runtime):
                    continue
                runtime.verify(include_records=False)
                guard = OwnerSendGuard(runtime, client)
                onebot = OwnerOneBot(guard, protocol)
            worked = await deliver_tick(client, onebot, spool, metrics=metrics, guard=guard)
            if not worked and runtime is not None and guard.segment_not_before is None:
                permission = await runtime.check(client)
                if "account_actions" in permission["capability_policies"]:
                    worked = await deliver_tick(client, onebot, spool, metrics=metrics,
                                                guard=guard, delivery_lane="qq_account")
        except httpx.HTTPError:
            metrics.emit("send_unavailable")
            await pause(stop, 1)
            continue
        if not worked:
            delay = onebot.config.poll_seconds
            if guard is not None and guard.segment_not_before is not None:
                remaining = (guard.segment_not_before - runtime.clock()).total_seconds()
                delay = min(delay, max(0.0, remaining))
            await pause(stop, delay)


async def transfer_media(pump, stop):
    try:
        while not stop.is_set():
            try:
                pump.tick()
                if pump.task is not None:
                    pump.window.verify()
                    await pump.task
                else:
                    await pause(stop, pump.window.config.poll_seconds)
            except (TimeoutError, httpx.TransportError):
                if not hasattr(pump.window, "authority"):
                    raise
                # The failed transfer has unwound before this await returns. Drop
                # its task, preserving the durable phase for native reconciliation;
                # never repeat a claim, download or upload from this error handler.
                await pump.close()
                if pump.metrics is not None:
                    pump.metrics.emit("media_unavailable")
                await pause(stop, pump.window.config.poll_seconds)
    finally:
        await pump.close()


async def observe_profile(pump, stop, memes):
    try:
        while not stop.is_set():
            if pump.running:
                pump.window.verify()
            pending = pump.window.db.execute(
                "SELECT 1 FROM inbox WHERE state='pending' LIMIT 1").fetchone()
            # Match the interleaved host: periodic probes wait for discovery,
            # whose reserved resources remain charged if it is cancelled.
            pump.tick(idle=pending is None and not memes.running)
            delay = pump.window.config.poll_seconds
            if not pump.running:
                delay = max(delay, pump.next_run - time.monotonic())
            await pause(stop, delay)
    finally:
        await pump.close()


async def discover_memes(pump, stop, profile):
    try:
        while not stop.is_set():
            pump.tick(idle=not profile.running)
            await pause(stop, pump.owner.config.poll_seconds)
    finally:
        await pump.close()


async def run(config):
    if config.owner_authority is None:
        raise ValueError("OWNER_AUTHORITY_REQUIRED")
    stop, wake = asyncio.Event(), asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop.set())
    metrics = Metrics()
    with Spool(config) as spool:
        runtime = None
        if config.owner_authority is not None:
            from .owner_runtime import OwnerRuntime

            runtime = OwnerRuntime(spool)
            runtime.verify(include_records=False)
        async with (
            private_http_client(config.core_url, config.bridge_token) as core,
            private_http_client(config.onebot_http_url, config.onebot_token) as protocol,
        ):
            onebot = OneBotClient(config, protocol)
            from .owner_memes import MemePump
            from .pending_media import MediaPump
            from .profile_probe import ProfilePump
            from .scheduled_import import run_scheduler_loop, schedule_import_job

            media = MediaPump(runtime, core, protocol=onebot, metrics=metrics)
            profile = ProfilePump(runtime, core, protocol)
            memes = MemePump(runtime, core)
            status = await runtime.probe(core, protocol)
            if not status["connection_ready"]:
                raise ValueError("ONEBOT_NOT_READY")

            # Register scheduled import job
            schedule_import_job(
                runtime=runtime,
                core=core,
                onebot=onebot,
                wake=wake,
                enabled=config.scheduled_meme_import_enabled,
                loop=asyncio.get_running_loop(),
            )

            async with asyncio.TaskGroup() as group:
                tasks = [
                    group.create_task(receive(config, spool, wake, stop, metrics, onebot,
                                              runtime=runtime, core=core)),
                    group.create_task(upload(core, spool, wake, stop, metrics,
                                             runtime=runtime, memes=memes, onebot=onebot)),
                    group.create_task(deliver(core, onebot, spool, stop, metrics,
                                              runtime=runtime, protocol=protocol)),
                    group.create_task(transfer_media(media, stop)),
                    group.create_task(observe_profile(profile, stop, memes)),
                    group.create_task(discover_memes(memes, stop, profile)),
                    group.create_task(run_scheduler_loop(stop)),
                ]
                await stop.wait()
                clean = await drain(tasks)
        return shutdown_report(runtime, clean)


def shutdown_report(runtime, clean):
    """What the stopped process leaves behind; the release script reads the exit code.

    An attempt that started I/O without a platform result is never replayed: the next
    process stops the authority and the delivery goes to reconciliation.
    """
    from . import held_inputs, journal

    plan = journal.digest(runtime.authority)
    attempts = dict(runtime.db.execute(
        "SELECT state,COUNT(*) FROM window_attempts WHERE plan_hash=? AND state IN "
        "('RESERVED','IO_STARTED','UNKNOWN') GROUP BY state", (plan,)))
    report = {"status": "CLEAN" if clean and not attempts else "UNSETTLED",
              "cancelled": not clean, "unfinished_sends": attempts,
              "pending_inputs": runtime.spool.pending_count,
              "pending_results": runtime.db.execute(
                  "SELECT COUNT(*) FROM results WHERE state='pending'").fetchone()[0],
              "held_inputs": held_inputs.counts(runtime).get("HELD", 0)}
    emit("bridge_shutdown_report", **report)
    return report


def main():
    import argparse

    class PrivateParser(argparse.ArgumentParser):
        def error(self, message):
            print(json.dumps({"state": "STOPPED", "error": "INVALID_BRIDGE_ARGUMENTS"}), flush=True)
            raise SystemExit(2)

    parser = PrivateParser(description="Single-owner OneBot v11 bridge")
    parser.add_argument(
        "--config", type=Path, default=os.environ.get("ONEBOT_BRIDGE_CONFIG", "/run/bridge.toml")
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="daemon",
        choices=(
            "daemon",
            "owner-meme-accounting-drain",
            "owner-status",
            "owner-stop",
            "owner-delivery-prepare",
            "owner-delivery-reconcile",
            "owner-handoff-prepare",
            "owner-carry-handoff-prepare",
            "owner-handoff",
            "owner-cutover",
            "window-open",
            "window-handoff",
            "window-abort-handoff",
            "window-ingest",
            "window-capture-once",
            "window-approve-input",
            "window-send-once",
            "window-run",
            "window-status",
            "window-stop",
            "window-quota-inventory",
            "window-delivery-prepare",
            "window-delivery-reconcile",
            "memory-control-once",
        ),
    )
    parser.add_argument("--window", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--event", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--memory-control-sha256")
    parser.add_argument("--message-id")
    parser.add_argument("--carry-sha256")
    parser.add_argument("--successor-policy-sha256")
    args = parser.parse_args()
    try:
        config = BridgeConfig.load(args.config)
        if args.command == "daemon":
            report = asyncio.run(run(config))
            if report["status"] != "CLEAN":
                raise SystemExit(3)
        elif args.command == "owner-meme-accounting-drain":
            from .meme_accounting_drain import command

            print(json.dumps(asyncio.run(command(config, args))), flush=True)
        elif args.command in {"owner-status", "owner-stop"}:
            from .owner_runtime import command

            print(json.dumps(command(config, args)), flush=True)
        elif args.command in {"owner-delivery-prepare", "owner-delivery-reconcile"}:
            from .owner_native_resolution import command

            print(json.dumps(asyncio.run(command(config, args))), flush=True)
        elif args.command in {
                "owner-handoff-prepare", "owner-carry-handoff-prepare", "owner-handoff"}:
            from .owner_handoff import command

            print(json.dumps(asyncio.run(command(config, args))), flush=True)
        elif args.command == "owner-cutover":
            from .owner_cutover import command

            print(json.dumps(asyncio.run(command(config, args))), flush=True)
        elif args.command == "memory-control-once":
            from .memory_control import command

            print(json.dumps(asyncio.run(command(config, args))), flush=True)
        else:
            from .window import command

            print(json.dumps(asyncio.run(command(config, args))), flush=True)
    except KeyboardInterrupt:
        return
    except Exception as error:
        # Never print transport exceptions: URLs, response bodies and headers are private.
        from .capture import diagnostic_fields

        unconfirmed = (isinstance(error, ValueError)
                       and error.args == ("LIVE_TEXT_CORE_STOP_UNCONFIRMED",))
        pending = isinstance(error, ValueError) and error.args == ("WINDOW_STOP_PENDING",)
        print(json.dumps({
            "state": "STOP_REQUESTED" if pending else (
                "CORE_STOP_UNCONFIRMED" if unconfirmed else "STOPPED"
            ),
            "error": "WINDOW_STOP_PENDING" if pending else (
                "LIVE_TEXT_CORE_STOP_UNCONFIRMED" if unconfirmed else "BRIDGE_FAILED_CLOSED"
            ),
            **diagnostic_fields(error),
        }), flush=True)
        raise SystemExit(1) from None

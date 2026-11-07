import asyncio
import hashlib
import json
import os
import sys
from time import monotonic

from pydantic import SecretStr

from companion.owner_authority_protocol import OwnerCapture
from companion_delivery_protocol.authority import capture_signing_payload
from tests.support.owner_input import inbound


async def start(
    core, directory, role="worker", *, duration=5, settle=0.4, controlled=False,
):
    directory.mkdir(parents=True)
    config, ready, report = (directory / name for name in ("config.json", "ready", "result.json"))
    config.write_text(
        json.dumps(
            {
                "settings": core.settings.model_dump(mode="python"),
                "role": role,
                "duration": duration,
                "settle": settle,
                "ready": str(ready),
                "report": str(report),
                "release": str(directory / "release") if controlled else None,
                "scan_progress": str(directory / "scans") if controlled else None,
            },
            default=lambda v: v.get_secret_value() if isinstance(v, SecretStr) else str(v),
        ),
        encoding="utf-8",
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-c",
        # Load SDK type declarations before the child instruments provider HTTP.
        "import pydantic_ai; from tests.worker_wakeup_process import main; main()",
        str(config),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return process, ready, report


async def ready(child):
    await wait_progress(child, child[1].exists, "ready", 15)


async def wait_progress(child, observed, expected, seconds):
    process, ready_path, report = child
    try:
        async with asyncio.timeout(seconds):
            while process.returncode is None:
                if observed():
                    return
                await asyncio.sleep(0.02)
    except TimeoutError:
        pass
    exit_at_failure = process.returncode
    # Let the bounded child snapshot its last native phase before draining stderr.
    ready_path.with_name("release").touch()
    drain = asyncio.create_task(process.communicate())
    try:
        _, stderr = await asyncio.wait_for(asyncio.shield(drain), 2)
    except TimeoutError:
        if process.returncode is None:
            process.kill()
        _, stderr = await drain
    result = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
    scans = ready_path.with_name("scans")
    raise AssertionError({
        "expected": expected, "exit_at_failure": exit_at_failure, "returncode": process.returncode,
        "ready": ready_path.exists(), "phase": result.get("phase", "no final report"),
        "last_completed_scan": scans.read_text() if scans.exists() else None,
        "counters": result.get("total"), "stderr": stderr.decode(errors="replace")[-4000:],
    })


async def finish(child, *, code=0):
    process, _, report = child
    output, error = await asyncio.wait_for(process.communicate(), 15)
    assert process.returncode == code, (
        output.decode(errors="replace"),
        error.decode(errors="replace"),
    )
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["pid"] != os.getpid()
    assert result["watchers"] == result["closed_watchers"]
    return result


async def close_child(child):
    process = child[0]
    if process.returncode is None:
        process.kill()
    await process.communicate()


def signed_input(core, template, key, number):
    value = template.model_dump(mode="json", exclude={"signature"})
    source = template.source_event_id.rpartition(":")[0] + ":" + str(number)
    stamp = core.clock()
    value.update(source_event_id=source,
                 event_id="evt_" + hashlib.sha256(source.encode()).hexdigest(),
                 occurred_at=stamp, received_at=stamp)
    value["signature"] = key.sign(capture_signing_payload(value)).hex()
    capture = OwnerCapture.model_validate(value)
    message = inbound(core, capture).model_copy(update={"occurred_at": capture.occurred_at})
    return capture, message


async def state(core, job_id, wanted="SUCCEEDED", seconds=3):
    started = monotonic()
    async with asyncio.timeout(seconds):
        while core.store.one((  # noqa: ASYNC110 - observes an independent process
            'SELECT state FROM jobs WHERE job_id=%s'
        ), (job_id,))["state"] != wanted:
            await asyncio.sleep(0.01)
    return monotonic() - started

import asyncio
import json
import os
import sys


def process_config(core, path):
    values = core.settings.model_dump(mode="json")
    assert core.settings.model_key.get_secret_value() == "synthetic-key"
    for name in ("model_key", "bridge_token", "control_token", "database"):
        values[name] = getattr(core.settings, name).get_secret_value()
    with open(path, "w", encoding="utf-8",
              opener=lambda filename, flags: os.open(filename, flags, 0o600)) as stream:
        json.dump(values, stream)


async def processes(core, directory, *, count=2, mode="normal", drain=False):
    directory.mkdir()
    config = directory / "synthetic-config.json"
    process_config(core, config)
    gate = directory / "go"
    children, logs = [], []
    try:
        for index in range(count):
            ready, log = directory / f"ready-{index}", directory / f"calls-{index}.jsonl"
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-B", "-m", "tests.evaluation_process", "--config", str(config),
                "--now", core.clock(), "--ready", str(ready), "--gate", str(gate),
                "--log", str(log), "--mode", mode,
                *(["--drain"] if drain else []),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            children.append(child)
            logs.append(log)
        async with asyncio.timeout(30):
            while not all((directory / f"ready-{i}").exists() for i in range(count)):
                stopped = [child for child in children if child.returncode is not None]
                if stopped:
                    errors = [err.decode(errors="replace") for _, err in
                              await asyncio.gather(*(child.communicate() for child in stopped))]
                    raise AssertionError("worker setup failed: " + "\n".join(errors))
                await asyncio.sleep(0.02)
            gate.write_text("go", encoding="utf-8")
            outputs = await asyncio.gather(*(child.communicate() for child in children))
        assert all(child.returncode == (23 if mode == "crash" else 0) for child in children), [
            (child.returncode, err.decode())
            for child, (_, err) in zip(children, outputs, strict=True)]
        return [json.loads(line)["payload_sha256"] for log in logs if log.exists()
                for line in log.read_text("utf-8").splitlines()]
    finally:
        for child in children:
            if child.returncode is None:
                child.kill()
                await child.wait()

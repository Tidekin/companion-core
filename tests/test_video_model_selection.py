"""An authenticated Console switch while the same independent worker serves two GIFs."""
import asyncio
import json
import pickle
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr

from companion.store import encode
from companion.video_input import VideoCandidate
from tests.conftest import Clock, run
from tests.sdk_reader_fixture import Provider, worker_for
from tests.test_native_video import animation, responses
from tests.test_owner_console import ORIGIN, PASSWORD
from tests.test_prepare_owner_input import window


async def resident(config_file):
    settings, now = pickle.loads(await asyncio.to_thread(Path(config_file).read_bytes))
    clock = Clock()
    clock.now = now
    provider = Provider(*responses(), *responses())
    wires = []

    def respond(request):
        if request.url.host in {"video.example.test", "second.example.test"}:
            if not wires:
                print("VIDEO", flush=True)
                assert input() == "continue"
            key = "synthetic-video-provider-key" if not wires else "alternate-video-key"
            assert request.headers["authorization"] == "Bearer " + key
            wires.append((request.url.host, json.loads(request.content)["model"]))
        return provider(request)

    async with worker_for(SimpleNamespace(settings=settings, clock=clock, store=None),
                          respond) as worker:
        fail = worker.fail_job

        def diagnose(job, code):
            traceback.print_exception(sys.exception(), file=sys.stderr)
            return fail(job, code)

        worker.fail_job = diagnose
        for _ in range(2):
            job_id = await asyncio.to_thread(input)
            assert await worker.run_once("resident-video-worker", job_id=job_id)
            row = worker.store.one("SELECT state,last_error_code FROM jobs WHERE job_id=?",
                                   (job_id,))
            assert row["state"] == "SUCCEEDED", row
            print("DONE", flush=True)
    assert wires == [("video.example.test", "configurable-video-one"),
                     ("second.example.test", "second-model")]


def test_console_switch_pins_inflight_route_and_prices_new_video(core, tmp_path):
    async def scenario():
        core.settings.operator_hot_reload = True
        core.settings.console.enabled = True
        core.settings.console.public_origin = ORIGIN
        core.settings.control_token = SecretStr(PASSWORD)
        route = VideoCandidate(provider="second", endpoint="https://second.example.test/v1",
            model="second-model", key=SecretStr("alternate-video-key"),
            input_rate=500000, cached_input_rate=50000, output_rate=1000000)
        async with window(core, tmp_path, media=True, content=True, video=True,
                          video_candidates={"second": route}) as (client, config, plan):
            async def gif(number):
                event = await animation(core, client, config, plan, tmp_path,
                                        "image/gif", number=number)
                return core.store.one("SELECT job_id FROM jobs WHERE event_id=?",
                                      (event["event_id"],))["job_id"]

            login = await client.post("/v1/console/session", json={"password": PASSWORD},
                                      headers={"Origin": ORIGIN})
            assert login.status_code == 200, login.text
            headers = {"Origin": ORIGIN, "Cookie": login.headers["set-cookie"].split(";", 1)[0],
                       "X-CSRF-Token": login.json()["csrf_token"]}

            async def save(version, change, operation):
                return await client.put("/v1/console/configuration",
                    headers={**headers, "Idempotency-Key": operation}, json={
                        "expected_version": version, "changes": {"video_models": change}})

            first = await gif(42)
            config_file = tmp_path / "worker.pickle"
            config_file.write_bytes(pickle.dumps((core.settings, core.clock.now)))
            proc = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m", __name__,
                str(config_file), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)

            def send(line):
                proc.stdin.write((line + "\n").encode())

            async def expect(expected):
                async with asyncio.timeout(65):
                    while line := await proc.stdout.readline():
                        if line.strip() == expected:
                            return
                    raise AssertionError((await proc.stderr.read()).decode(errors="replace"))

            try:
                send(first)
                await expect(b"VIDEO")
                data = route.model_dump(exclude={"key"}) | {"key": route.key.get_secret_value()}
                saved = await save(0, {"candidates": {"second": data}}, "create-video-candidate")
                assert saved.status_code == 200, saved.text
                assert route.key.get_secret_value() not in saved.text
                field = next(f for f in saved.json()["fields"] if f["key"] == "video_models")
                assert field["authorized"] == ["second"]
                denied = await save(1, {"selected": "second", "candidates": {"second": {
                    "input_rate": 600000}}}, "deny-unapproved-video")
                assert denied.status_code == 400
                selected = await save(1, {"selected": "second"}, "activate-approved-video")
                assert selected.status_code == 200, selected.text
                send("continue")
                await expect(b"DONE")
                second = await gif(43)
                send(second)
                await expect(b"DONE")
                await asyncio.wait_for(proc.wait(), 20)
                assert proc.returncode == 0, (await proc.stderr.read()).decode(errors="replace")
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            rows = core.store.rows("SELECT m.model,m.state,m.billed_micro_usd,a.measurement_json "
                "FROM model_usage m JOIN model_admissions a USING(request_id) "
                "WHERE m.scenario='content_video' ORDER BY m.rowid")
            assert [(r["model"], r["state"], r["billed_micro_usd"]) for r in rows] == [
                ("configurable-video-one", "SETTLED", 140), ("second-model", "SETTLED", 70)]
            assert json.loads(rows[1]["measurement_json"])["video_profile"] == "second"
            assert route.key.get_secret_value() not in encode(rows)
    run(scenario())


if __name__ == "__main__":
    asyncio.run(resident(sys.argv[1]))

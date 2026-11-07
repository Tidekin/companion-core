import asyncio
import pickle
import sys


async def child(tmp_path, name, core, **options):
    path = tmp_path / (name + ".pickle")
    bundle = {"settings": core.settings, "now": core.clock.now, **options}
    path.write_bytes(pickle.dumps(bundle))
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.model_carry_native_host",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


async def finished(process):
    output, _ = await asyncio.wait_for(process.communicate(), 40)
    assert process.returncode == 0, output.decode(errors="replace")
    return output.decode().strip()

import asyncio

PREFIX = "/internal/v1"


def headers(core):
    return {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}


async def event_wait(event):
    assert await asyncio.to_thread(event.wait, 3), "worker did not reach bounded test gate"


async def child_done(process):
    try:
        output, error = await asyncio.wait_for(process.communicate(), 40)
        assert process.returncode == 0, (output + error).decode(errors="replace")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

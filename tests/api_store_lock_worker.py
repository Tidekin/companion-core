"""Hold the native privacy row from an independent process until the test releases it."""

import asyncio
import json
import sys
from contextlib import closing

from pydantic import SecretStr

from companion.postgres import Database
from companion.store import Store


async def start(core):
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-B", "-m", "tests.api_store_lock_worker",
        str(core.settings.state_directory),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    child.stdin.write((json.dumps(core.settings.database.get_secret_value()) + "\n").encode())
    await child.stdin.drain()
    return child


if __name__ == "__main__":
    dsn = SecretStr(json.loads(sys.stdin.readline()))
    with (Database(dsn) as database, closing(Store(database, sys.argv[1])) as store,
          store.transaction(control_write=True)):
        print("LOCKED", flush=True)
        assert sys.stdin.readline() == "release\n"

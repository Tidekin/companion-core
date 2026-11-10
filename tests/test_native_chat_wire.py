"""The shipped client over TLS sockets, followed by a fresh worker process; no QQ service."""

import asyncio
import json
import os
import pickle
import sys
from pathlib import Path

import pytest

from tests.conftest import run
from tests.support.native_chat_tls import serve
from tests.support.owner_console import PASSWORD
from tests.support.owner_input import captured


@pytest.mark.skipif(os.environ.get('COMPANION_FLUTTER_WIRE') != '1',
    reason='explicit Flutter/Core cross-runtime gate; run scripts/check-flutter.ps1 -Wire')
def test_flutter_tls_entry_worker_and_restart_without_qq(core, tmp_path):
    captured(core, tmp_path)
    directory = tmp_path / 'flutter-client'
    directory.mkdir()
    root = Path(os.environ['COMPANION_FLUTTER_WORKSPACE']) if os.environ.get(
        'COMPANION_FLUTTER_WORKSPACE') else Path(__file__).resolve().parents[1]
    project = root / 'clients/chat'

    async def scenario():
        async with serve(core, tmp_path) as (origin, certificate):
            config = directory / 'wire.private.json'

            async def client(phase):
                config.write_text(json.dumps(dict(phase=phase, origin=origin,
                    certificate=str(certificate), password=PASSWORD, directory=str(directory),
                    now=core.clock.now.isoformat())), encoding='utf-8')
                env = {**os.environ, 'COMPANION_WIRE_CONFIG': str(config)}
                process = await asyncio.create_subprocess_exec('pwsh', '-NoProfile', '-File',
                    str(root / 'scripts/flutter.ps1'), '-Tool', 'flutter',
                    'test', 'test/wire_entry.dart',
                    cwd=project, env=env, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT)
                try:
                    logs, _ = await asyncio.wait_for(process.communicate(), 180)
                except BaseException:
                    process.kill()
                    await process.wait()
                    raise
                assert process.returncode == 0, logs.decode(errors='replace')

            await client('initial')
            receipt = json.loads((directory / 'receipt.json').read_text(encoding='utf-8'))
            client_task = asyncio.create_task(client('stream'))
            async with asyncio.timeout(90):
                while not (directory / 'subscription-ready').exists():
                    if client_task.done():
                        await client_task
                        raise AssertionError('client exited without subscribing')
                    await asyncio.sleep(0.05)
            job_id = core.store.one('SELECT job_id FROM jobs WHERE event_id=%s',
                                    (receipt['event_id'],))['job_id']
            worker_config = directory / 'worker.pickle'
            worker_config.write_bytes(pickle.dumps((core.settings, core.clock.now,
                                                   job_id, str(directory))))
            worker = await asyncio.create_subprocess_exec(sys.executable, '-B',
                str(root / 'tests/native_stream_worker.py'), str(worker_config),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                await client_task
                logs, _ = await asyncio.wait_for(worker.communicate(), 90)
                assert worker.returncode == 0, logs.decode(errors='replace')
            finally:
                if worker.returncode is None:
                    worker.kill()
                    await worker.wait()
            assert (directory / 'client-saw-preview').exists()
            await client('restart')
            assert len(core.store.rows('SELECT 1 FROM model_usage')) == 1

    run(scenario())

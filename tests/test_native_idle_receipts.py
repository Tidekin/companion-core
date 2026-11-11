"""An empty report queue does no custody replay; real reports still require it."""

from types import SimpleNamespace

import httpx
import pytest

from companion_onebot_bridge.owner_resolution import TABLE
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.support.bridge_owner_runtime import config as config


def test_idle_receipts_skip_history_but_keep_identity_and_pending_guard(config, monkeypatch):
    async def scenario():
        with Spool(config) as spool:
            runtime = OwnerRuntime(spool)
            # An existing, empty reconciliation schema, without fabricated proofs.
            spool.db.execute(f"CREATE TABLE {TABLE} (message_id TEXT PRIMARY KEY,"
                "plan_hash TEXT NOT NULL,proof_json TEXT NOT NULL,receipt_json TEXT,"
                "receipt_record_sha256 TEXT)")
            runtime.verify()
            original, checks, requests = runtime.verify, [], []

            def verify(**kwargs):
                checks.append(kwargs)
                return original(**kwargs)

            def response(request):
                requests.append(request.url.path)
                pytest.fail("empty or corrupt custody must not submit a report")

            monkeypatch.setattr(runtime, "verify", verify)
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
                for _ in range(3):
                    assert not await spool.flush_result(client, runtime=runtime)
                assert checks == [] and requests == []
                with pytest.raises(ValueError, match="^OWNER_RECEIPT_SPOOL_REQUIRED$"):
                    await spool.flush_result(client, runtime=SimpleNamespace(spool=object()))
                with spool.db:
                    spool.db.execute("INSERT INTO results VALUES (?,?,'pending')",
                                     ("msg_" + "a" * 32, '{"result":"SENT"}'))
                runtime.path.write_bytes(runtime.path.read_bytes() + b"truncated")
                with pytest.raises(ValueError):
                    await spool.flush_result(client, runtime=runtime)
                assert checks == [{"active": False, "include_records": False}] and requests == []

    run(scenario())

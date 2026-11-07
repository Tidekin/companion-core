from dataclasses import replace

from companion_onebot_bridge import owner_handoff
from tests.native_owner_recovery_support import reopen_host
from tests.support.owner_authority import activate


def reopen(env):
    return reopen_host(env.active, env.monkeypatch)


async def successor(env, old):
    await owner_handoff.prepare(old, env.client)
    approved, _ = activate(env.core, env.policy, revision=old.authority["revision"] + 1,
                           operation="native-recovery-successor")
    result = await owner_handoff.handoff(old, env.client, approved["authority"])
    assert result["state"] == "ACTIVE"
    return replace(env.cfg, owner_authority=approved["authority"])


def original_rows(runtime, table):
    return owner_handoff._rows(runtime.db, f"SELECT * FROM {table} ORDER BY rowid")


def assert_custody_counts(runtime, *, inputs=1, sends=1):
    records = runtime.verify(active=False)
    assert sum(row["kind"] == "CAPTURED" for row in records) == inputs
    assert sum(row["kind"] == "RESERVED" for row in records) == sends
    assert runtime.db.execute("SELECT COUNT(*) FROM window_inputs").fetchone()[0] == inputs
    assert runtime.db.execute("SELECT COUNT(*) FROM window_attempts").fetchone()[0] == sends

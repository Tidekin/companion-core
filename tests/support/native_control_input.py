"""Control-message scenarios reuse the native signed-input fixture unchanged."""

from contextlib import asynccontextmanager
from unittest.mock import patch

from companion import owner_authority
from tests import native_input_protocol_fixture as native
from tests.support.owner_authority import activate


@asynccontextmanager
async def environment(core, tmp_path, monkeypatch):
    async with native.environment(core, tmp_path, monkeypatch) as values:
        client, config, grant, now = values
        assert not grant.captures
        current = owner_authority.current(core.store, core.settings, core.clock)
        # Memory review has its own native model purpose. Approve it before any
        # capture rather than adding permission to a previously signed source.
        policy = current.policy.model_copy(update={
            "purposes": ["chat", "memory", "diary"], "memory_scope": "owner_private"})
        grant.authority = activate(core, policy, revision=grant.authority["revision"],
            operation="native-control-memory-scope")[0]["authority"]
        yield client, config, grant, now


async def admit(client, config, grant, now, number, text):
    original = native.raw_event

    def message(*args, **kwargs):
        return original(*args, **{**kwargs, "text": text})

    # Customize only the synthetic OneBot frame; signing, HTTP admission and
    # grant capture retention are exactly the shared fixture's implementation.
    with patch.object(native, "raw_event", message):
        event, accepted = await native.admit(client, config, grant, now, number)
    assert accepted.status_code == 202, accepted.text
    return event, accepted.json()

"""A frozen window also covers code moved into shared media/protocol packages."""

from pathlib import Path

import pytest

from companion import live_text
from companion.contracts import CoreError
from tests.conftest import run
from tests.support.onebot_capture import source_window


@pytest.mark.parametrize("package", ["companion_delivery_protocol", "companion_voice_node"])
def test_shared_runtime_change_closes_the_original_window(core, tmp_path, monkeypatch, package):
    async def source(ws):
        await ws.wait_closed()

    async def scenario():
        async with source_window(core, tmp_path, source, live=True, approved=True):
            live_text.guard(core.store, core.settings, core.clock)
            original = Path.read_bytes

            def changed(path):
                value = original(path)
                return value + b"\n# synthetic changed runtime\n" if (
                    path.parent.name == package) else value

            monkeypatch.setattr(Path, "read_bytes", changed)
            with pytest.raises(CoreError, match="LIVE_TEXT_RUNTIME_CHANGED"):
                live_text.guard(core.store, core.settings, core.clock)
            assert not core.store.rows("SELECT * FROM model_usage")
            assert not core.store.rows("SELECT * FROM outbox")

    run(scenario())

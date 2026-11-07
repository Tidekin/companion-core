from contextlib import asynccontextmanager

from companion_onebot_bridge.meme_runtime import MemeRuntime
from tests.support.meme_asset_migration import config_file
from tests.support.meme_core import worker
from tests.support.meme_responsibility_bridge import bridge_case


def config_path(c, tmp_path):
    config = c.owner.config
    return config_file(
        tmp_path / "accounting.toml",
        {
            key: getattr(config, key)
            for key in (
                "owner_qq_id",
                "bot_qq_id",
                "bridge_token",
                "onebot_token",
                "protocol_epoch",
                "data_dir",
                "core_url",
                "capture_signing_key",
                "owner_authority",
                "meme_enabled",
            )
        },
    )


@asynccontextmanager
async def stopped_case(core, tmp_path, monkeypatch, *, state="SETTLED"):
    async with bridge_case(core, tmp_path, monkeypatch) as c:
        MemeRuntime(c.owner)
        calls = []

        async def lost():
            raise ValueError("Synthetic loss after actual model dispatch")

        assert await worker(core, calls, hook=lost if state == "UNKNOWN" else None)
        assert len(calls) == 1
        c.owner.stop()
        c.path = config_path(c, tmp_path)
        c.database = c.owner.spool.directory / "spool.sqlite3"
        c.directory = c.owner.spool.directory
        c.config = c.owner.config
        c.owner.spool.close()
        yield c

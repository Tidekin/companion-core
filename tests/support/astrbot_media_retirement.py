import json
from dataclasses import asdict

from astrbot_plugin_companion_bridge.bridge import Spool as LegacySpool
from astrbot_plugin_companion_bridge.bridge import normalize as legacy_normalize
from tests.support.content_bridge import config as plugin_config
from tests.support.content_bridge import event

BOUNDARY = 1792382399


def private_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def native_file(path, cfg):
    path.write_text("\n".join(f"{key} = {json.dumps(value)}" for key, value in asdict(cfg).items()
                              if value is not None) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def old_spool(tmp_path, cfg, *, pending=False):
    path = tmp_path / "inbox.sqlite3"
    spool = LegacySpool(str(path))
    spool.bind_backend("napcat", "old-generation")
    key, payload = legacy_normalize(event([], text="previous text"), cfg.owner_qq_id,
                                    cfg.bot_qq_id, protocol_epoch="old-generation")
    payload["payload"]["shared_content"] = [{"kind": "image", "slot": 0}]
    if pending:
        spool.put(key, payload)
    else:
        spool.bind_origin(payload)
    spool.close()
    plugin = plugin_config(bridge_token=cfg.bridge_token, core_url=cfg.core_url,
                           protocol_epoch="old-generation", content_enabled=True)
    return path, private_json(tmp_path / "old.json", plugin), key, payload

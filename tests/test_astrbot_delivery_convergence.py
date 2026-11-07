"""Legacy receipt migration and isolated packaging after host ingress retirement."""

import json
import sqlite3
import subprocess
import sys
import zipfile

import pytest

from astrbot_plugin_companion_bridge.bridge import Spool, normalize
from companion_onebot_bridge.delivery_receipts import DeliveryReceipts
from companion_onebot_bridge.spool import Spool as OneBotSpool
from tests.support.content_bridge import event


def test_original_inbox_migration_preserves_identity_epoch_and_idempotency(tmp_path):
    path = str(tmp_path / "inbox.sqlite3")
    key, payload = normalize(event([]), "10001", "10002", protocol_epoch="old-epoch")
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE inbox(key TEXT PRIMARY KEY,body TEXT,state TEXT);"
                         "CREATE TABLE protocol_generations(epoch TEXT PRIMARY KEY,backend TEXT);")
        db.execute("INSERT INTO inbox VALUES (?,?,'pending')", (key, body))
        db.execute("INSERT INTO protocol_generations VALUES ('old-epoch','napcat')")
    spool = Spool(path)
    try:
        assert spool.delivery_identity("10001", "10002") == "qq:FriendMessage:10001"
        assert spool.db.execute("SELECT key,body,state FROM inbox").fetchone() == (
            key, body, "pending")
        assert spool.db.execute("SELECT * FROM results").fetchall() == []
        with pytest.raises(ValueError):
            spool.delivery_identity("99999", "10002")
        with pytest.raises(ValueError):
            spool.bind_backend("snowluma", "new-epoch")
        with pytest.raises(ValueError):
            spool.check_backend("snowluma", "old-epoch")
    finally:
        spool.close()


def test_both_hosts_share_the_same_receipt_implementation():
    assert Spool.start_delivery is OneBotSpool.start_delivery is DeliveryReceipts.start_delivery
    assert Spool.flush_result is OneBotSpool.flush_result is DeliveryReceipts.flush_result


def test_plugin_zip_contains_shared_dispatch_without_core(tmp_path):
    from scripts.package_bridge import package_bridge

    archive = package_bridge(tmp_path / "bridge.zip")
    target = tmp_path / "isolated_plugin"
    with zipfile.ZipFile(archive) as package:
        package.extractall(target)
        assert not any(name.startswith("companion/") for name in package.namelist())
        assert "content.py" not in package.namelist() and "delivery.py" not in package.namelist()
    script = '''
import importlib.abc
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'companion', 'companion_onebot_bridge',
                'companion_delivery_protocol', 'companion_meme_protocol',
                'companion_meme_sources'}:
            raise AssertionError('Unbundled runtime import: ' + fullname)
sys.meta_path.insert(0, Guard())
from isolated_plugin.bridge import Spool
from isolated_plugin._transport.delivery_receipts import DeliveryReceipts
from isolated_plugin._transport.outbound import deliver_item
from isolated_plugin import _meme_sources as sources
from isolated_plugin._meme_sources import community, fetch, review
from isolated_plugin._transport import images, meme_catalog, meme_discovery, meme_runtime
assert Spool.flush_result is DeliveryReceipts.flush_result
assert callable(deliver_item)
assert meme_catalog.Metadata is review.Metadata is sources.Metadata
assert meme_discovery.ReviewApproval is review.ReviewApproval is sources.ReviewApproval
assert meme_discovery.PublicFetcher is fetch.PublicFetcher
assert meme_runtime.CommunityPacks is community.CommunityPacks
assert meme_runtime.TrustedReviewer is review.TrustedReviewer
assert images.resolve_public is fetch.resolve_public
for module in (sources, community, fetch, review, images, meme_catalog,
               meme_discovery, meme_runtime):
    assert Path(module.__file__).resolve().is_relative_to(
        Path(sys.argv[1]) / 'isolated_plugin')
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(tmp_path)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

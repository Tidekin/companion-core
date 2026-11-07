import base64
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

from companion.meme_core import SPEC
from companion.store import encode
from companion_meme_protocol import CaptionIdentity
from companion_meme_sources import Metadata
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.meme_catalog import MemeCatalog
from companion_onebot_bridge.meme_library import MemeLibrary
from companion_onebot_bridge.meme_semantic import SemanticLibrary
from companion_onebot_bridge.meme_semantic_http import CaptionHTTP, SemanticHTTP
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.support.meme_core import body, caption_environment
from tests.support.onebot_events import raw_event


@asynccontextmanager
async def bridge_case(core, tmp_path, monkeypatch):
    async with caption_environment(core, tmp_path, monkeypatch) as values:
        event, _, client, config, grant, now, attempt = values
        config = replace(config, data_dir=str(tmp_path / "native-bridge"),
            core_url=str(client.base_url).rstrip("/"), meme_enabled=True,
            capture_signing_key=grant.key.private_bytes_raw().hex(),
            owner_authority=grant.authority)
        payload = body(core, event, attempt, now)
        proof = payload["source_evidence"]
        meta = Metadata("like", "Synthetic untrusted caption hint", ("抱抱",), ("comfort",), (),
            **{k: proof[k] for k in (
                "source_page", "asset_url", "license", "usage_basis", "fetched_at", "revision")},
            acquisition_json=encode(proof["acquisition"]))
        with Spool(config) as spool:
            owner = OwnerRuntime(spool, clock=lambda: now)
            raw = json.dumps(raw_event(1, text="合成主人输入1", time=int(now.timestamp())))
            capture = normalize(raw, config, now=now.timestamp())
            assert capture["event_id"] == event["event_id"]
            assert owner.persist_capture(capture, raw, received_at=now, peer=("127.0.0.1", 3001))
            catalog = MemeCatalog(spool.db, MemeLibrary(tmp_path, enabled=True))
            sha = catalog.add(base64.b64decode(payload["asset"]["data_base64"]), meta,
                              check=lambda: owner.verify())
            semantics = SemanticLibrary(catalog, CaptionIdentity(**payload["identity"]), SPEC)

            async def check():
                owner.verify()

            async with private_http_client(config.core_url, config.bridge_token) as private:
                adapter = CaptionHTTP(SemanticHTTP(owner, private, check=lambda: owner.verify()),
                                      catalog)
                pending = await semantics.advance(sha, adapter, payload["context"], check=check,
                                                  commit_check=lambda: owner.verify())
                assert pending["state"] == "PENDING"
                yield SimpleNamespace(owner=owner, catalog=catalog, semantics=semantics,
                    adapter=adapter, client=private, context=payload["context"], sha=sha,
                    check=check, core_client=client, grant=grant, job_id=spool.db.execute(
                        "SELECT job_id FROM meme_caption_bindings").fetchone()[0])

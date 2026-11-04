"""Actual private TCP and native Bridge custody; no settlement/content consumer changes."""

import base64
import copy
import json
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

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
from tests.conftest import run
from tests.support.onebot_events import raw_event
from tests.test_meme_core import body, caption_environment, worker
from tests.test_meme_responsibility import snapshot


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


def bridge_snapshot(c):
    return tuple(c.catalog.db.iterdump()), c.owner.path.read_bytes()


def test_original_capture_and_fee_remain_verifiable_with_only_caption_bodies_cleared_in_clone(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with bridge_case(core, tmp_path, monkeypatch) as c:
            assert await worker(core, [])
            assert (await c.semantics.advance(c.sha, c.adapter, c.context, check=c.check,
                commit_check=lambda: c.owner.verify()))["state"] == "READY"
            before, native = snapshot(core), bridge_snapshot(c)
            expected = await c.adapter.get_responsibility(c.job_id)
            assert expected["liability_kind"] == "USAGE"
            with sqlite3.connect(tmp_path / "caption-body-clone.sqlite3") as clone:
                c.catalog.db.backup(clone)
                clone.execute("UPDATE meme_caption_bindings "
                              "SET context_json=NULL,outcome_json=NULL "
                              "WHERE job_id=?", (c.job_id,))
                clone.commit()
                owner, catalog = copy.copy(c.owner), copy.copy(c.catalog)
                owner.db, catalog.db = clone, clone
                adapter = CaptionHTTP(SemanticHTTP(owner, c.client, check=lambda: None), catalog)
                cloned_before = tuple(clone.iterdump())
                assert await adapter.get_responsibility(c.job_id) == expected
                with pytest.raises(ValueError, match="MEME_CAPTION_JOB_UNBOUND"):
                    await adapter.get(c.job_id)
                assert tuple(clone.iterdump()) == cloned_before
            assert snapshot(core) == before and bridge_snapshot(c) == native

    run(scenario())


@pytest.mark.parametrize("change", ["binding", "usage", "capture", "config", "journal"])
def test_actual_response_then_local_custody_change_refuses_without_a_consumer_write(
    core, tmp_path, monkeypatch, change
):
    async def scenario():
        async with bridge_case(core, tmp_path, monkeypatch) as c:
            assert await worker(core, [])
            before = snapshot(core)
            after_hook = []

            async def mutate(response):
                assert response.status_code == 200
                if change == "binding":
                    c.catalog.db.execute("UPDATE meme_caption_bindings SET source_sha=?",
                                         ("f" * 64,))
                elif change == "usage":
                    c.catalog.db.execute("UPDATE meme_caption_bindings SET usage_json='{}'")
                elif change == "capture":
                    c.catalog.db.execute("UPDATE window_inputs SET capture_json='{}'")
                elif change == "config":
                    c.owner.config = replace(c.owner.config,
                                             bot_qq_id="9999999988")
                else:
                    c.owner.path.write_bytes(c.owner.path.read_bytes() + b"\n")
                after_hook.append(bridge_snapshot(c))

            c.client.event_hooks["response"].append(mutate)
            with pytest.raises(ValueError):
                await c.adapter.get_responsibility(c.job_id)
            assert len(after_hook) == 1
            assert bridge_snapshot(c) == after_hook[0] and snapshot(core) == before

    run(scenario())


@pytest.mark.parametrize("change", ["authorization", "cookie", "body", "path"])
def test_request_hook_cannot_change_original_private_accounting_request(
    core, tmp_path, monkeypatch, change
):
    async def scenario():
        async with bridge_case(core, tmp_path, monkeypatch) as c:
            assert await worker(core, [])
            before, native = snapshot(core), bridge_snapshot(c)
            responses = []

            async def mutate(request):
                if change == "authorization":
                    request.headers["Authorization"] = "Bearer synthetic-wrong"
                elif change == "cookie":
                    request.headers["Cookie"] = "synthetic=1"
                elif change == "path":
                    request.url = request.url.copy_with(path="/internal/v1/health/live")
                else:
                    request._content = b'{"extra":true}'

            async def received(response):
                responses.append(response.status_code)

            c.client.event_hooks["request"].append(mutate)
            c.client.event_hooks["response"].append(received)
            with pytest.raises(ValueError):
                await c.adapter.get_responsibility(c.job_id)
            assert not responses
            assert snapshot(core) == before and bridge_snapshot(c) == native

    run(scenario())


def test_missing_native_capture_cannot_be_replaced_by_a_valid_core_receipt(
    core, tmp_path, monkeypatch
):
    async def scenario():
        async with bridge_case(core, tmp_path, monkeypatch) as c:
            assert await worker(core, [])
            c.catalog.db.execute("UPDATE window_inputs SET capture_json=NULL")
            before, native = snapshot(core), bridge_snapshot(c)
            with pytest.raises(ValueError):
                await c.adapter.get_responsibility(c.job_id)
            assert snapshot(core) == before and bridge_snapshot(c) == native

    run(scenario())


@pytest.mark.parametrize("changed", ["regression", "request_id", "schema", "identity", "authority"])
def test_actual_core_response_must_match_original_binding_and_monotonic_retained_usage(
    core, tmp_path, monkeypatch, changed
):
    async def scenario():
        async with bridge_case(core, tmp_path, monkeypatch) as c:
            assert await worker(core, [])
            assert (await c.semantics.advance(c.sha, c.adapter, c.context, check=c.check,
                commit_check=lambda: c.owner.verify()))["state"] == "READY"
            before, native = snapshot(core), bridge_snapshot(c)
            replies = []

            async def corrupt(response):
                assert response.status_code == 200
                await response.aread()
                value = response.json()
                replies.append(value["usage"]["state"])
                if changed == "regression":
                    value["usage"].update(state="UNKNOWN", billed_micro_usd=None)
                elif changed == "request_id":
                    value["usage"]["usage_request_id"] = "model_" + "f" * 32
                elif changed == "schema":
                    value["result"] = "Synthetic extra body refused"
                elif changed == "identity":
                    value["source_evidence_sha256"] = "f" * 64
                else:
                    value["authority_sha256"] = "f" * 64
                response._content = json.dumps(value).encode()

            c.client.event_hooks["response"].append(corrupt)
            with pytest.raises(ValueError):
                await c.adapter.get_responsibility(c.job_id)
            assert replies == ["SETTLED"]
            assert snapshot(core) == before and bridge_snapshot(c) == native

    run(scenario())


@pytest.mark.parametrize("case", ["stop", "erasure"])
def test_native_stop_and_core_erasure_keep_unknown_fee_without_rewriting_local_state(
    core, tmp_path, monkeypatch, case
):
    from tests.meme_authority_fixture import stop

    async def scenario():
        async with bridge_case(core, tmp_path, monkeypatch) as c:
            calls = []

            async def interrupt():
                if case == "stop":
                    response = await stop(c.core_client, c.grant)
                    assert response.status_code == 200, response.text
                    c.owner.stop()
                else:
                    with core.store.transaction():
                        core.ledger.expire_events(core.store, [c.context["event_id"]], core.clock())
                raise ValueError("Synthetic response loss after real dispatch")

            assert await worker(core, calls, hook=interrupt) and len(calls) == 1
            before, native = snapshot(core), bridge_snapshot(c)
            value = await c.adapter.get_responsibility(c.job_id)
            assert value["liability_kind"] == "USAGE" and value["usage"]["state"] == "UNKNOWN"
            assert value["usage_owner_job_id"] == c.job_id
            if case == "erasure":
                assert value["body_erased"] and value["job_state"] == "CANCELLED"
            assert snapshot(core) == before and bridge_snapshot(c) == native

    run(scenario())

"""A new owner turn may consume one original read; its provenance and bill stay fixed."""

import asyncio
import json
import ssl
import zlib

import httpcore
import httpx
import pytest
from cryptography.fernet import Fernet

from companion import owner_authority
from companion.backup import backup, restore
from companion.core import Core
from companion.model import ModelClient
from companion.owner_authority_protocol import AuthorityStop
from companion.privacy import advance_epoch
from companion.reading import FOLLOWUP, followup_item
from companion.store import encode
from source_reader import worker_v1
from source_reader.bilibili import BilibiliReader
from source_reader.network import PublicHTTP
from tests.conftest import run
from tests.support.exploration import URL
from tests.support.reading_followup import (
    READER_AUTH,
    SHORT,
    admit,
    finish,
    lease,
    notice,
    select_read,
    validate,
    window,
)
from tests.support.source_reader import META, PLAYER, SUBTITLE, Responses, response


@pytest.mark.parametrize("context_soft_tokens", [3200, 16000])
def test_signed_shortlink_read_continues_into_latest_reply_once(
    core, tmp_path, monkeypatch, context_soft_tokens,
):
    async def scenario():
        async with window(core, tmp_path, monkeypatch,
                          context_soft_tokens=context_soft_tokens) as (client, config, plan):
            first, old = await admit(client, core, config, plan, 1, card=SHORT)
            await select_read(core, SHORT)
            entered, release = asyncio.Event(), asyncio.Event()
            metadata = {**META, "data": {**META["data"], "aid": 1701, "duration": 12}}
            comments = {"code": 0, "data": {"page": {"count": 1}, "replies": [{
                "rpid": 101, "content": {"message": "评论层观点"},
            }]}}
            danmaku = b'<i><chatid>1234</chatid><d p="1.25,1,25,0">sample</d></i>'

            class PausedNetwork(Responses):
                async def connect_tcp(self, host, port, **kwargs):
                    if not self.hosts:
                        entered.set()
                        await release.wait()
                    return await super().connect_tcp(host, port, **kwargs)

            backend = PausedNetwork(
                response(status=302, headers={"Location": URL}), response(metadata),
                response(PLAYER), response(SUBTITLE), response(comments),
                response(zlib.compress(danmaku)),
            )
            pool = httpcore.AsyncConnectionPool(
                ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
                network_backend=backend, max_keepalive_connections=0,
            )
            calls = []

            def model_provider(request):
                calls.append(json.loads(request.content)["messages"])
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": "读到了部分字幕，恒星颜色与温度有关。"}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                })

            async with PublicHTTP(pool) as public, httpx.AsyncClient(
                base_url=str(client.base_url), headers=READER_AUTH, trust_env=False,
            ) as reader_http, httpx.AsyncClient(
                transport=httpx.MockTransport(model_provider),
            ) as model_http:
                task = asyncio.create_task(worker_v1.tick(
                    reader_http, BilibiliReader(public, clock=core.clock, discussion=True),
                    "bili", source="bilibili", clock=lambda: core.clock.now,
                ))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    frozen = core.store.one("SELECT * FROM reading_attempts")
                    latest, accepted = await admit(
                        client, core, config, plan, 2, "你试试应该可以看了",
                    )
                    assert accepted["reply_revision"] == old["reply_revision"] + 1
                    await select_read(core, SHORT)
                    with Core(core.settings, ModelClient(core.settings, model_http),
                              core.clock) as worker:
                        assert worker.store is not core.store
                        assert worker.claim_job("wait-for-original-reader") is None
                        assert core.store.one('SELECT state,attempts FROM jobs WHERE job_id=%s',
                                              (accepted["job_id"],)) == {
                            "state": "RETRY_WAIT", "attempts": 0,
                        }
                        release.set()
                        assert await asyncio.wait_for(task, 10)
                        assert await worker.run_once("current-consumer")
                        assert not await worker.run_once("no-repeat")
                    with Core(core.settings, ModelClient(core.settings, model_http),
                              core.clock) as restarted:
                        assert not await restarted.run_once("restart-no-repeat")
                    assert not await worker_v1.tick(
                        reader_http, BilibiliReader(public, clock=core.clock), "bili",
                        source="bilibili", clock=lambda: core.clock.now,
                    )
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            assert len(backend.requests) == 6 and not backend.rows
            attempt = core.store.one("SELECT * FROM reading_attempts")
            assert attempt["state"] == "DONE" and attempt["observed_requests"] == 6
            assert {key: attempt[key] for key in ("request_id", "context_json", "lease_token")} == {
                key: frozen[key] for key in ("request_id", "context_json", "lease_token")
            }
            read = core.store.one("SELECT * FROM exploration_requests")
            assert read["used_requests"] == 6 and read["used_bytes"] == attempt["observed_bytes"]
            observation = core.store.one("SELECT * FROM observations")
            evidence = json.loads(observation["evidence_json"])["read_observation"]
            assert evidence["provenance"] == {
                **json.loads(frozen["context_json"]), "source_grant": None,
            }
            assert evidence["provenance"]["source_event_ids"] == [first["event_id"]]
            assert len(calls) == 1
            prompt = encode(calls[0])
            assert all(value in prompt for value in (
                "恒星的颜色与温度有关", "你试试应该可以看了", "request_id", "FETCHED",
            ))
            if context_soft_tokens == 16000:
                assert "评论层观点" in prompt and "current_pool_sample" in prompt
            assert core.store.rows("SELECT origin_event_id FROM outbox") == [
                {"origin_event_id": latest["event_id"]},
            ]
            assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (old["job_id"],)) == {
                "state": "CANCELLED",
            }
            assert core.store.rows("SELECT scenario,state FROM model_usage") == [
                {"scenario": "chat", "state": "SETTLED"},
            ] * 3
            sources = {row["event_id"] for row in core.store.rows(
                'SELECT event_id FROM job_sources WHERE job_id=%s', (accepted["job_id"],),
            )}
            assert {first["event_id"], latest["event_id"], observation["event_id"]} <= sources
            assert not core.store.one('SELECT 1 FROM event_dependencies WHERE event_id=%s',
                                      (latest["event_id"],))
    run(scenario())


@pytest.mark.parametrize("stage", ["queued", "done", "UNKNOWN", "FAILED"])
def test_followup_reuses_existing_state_without_requeue(core, tmp_path, monkeypatch, stage):
    async def scenario():
        async with window(core, tmp_path, monkeypatch) as (client, config, plan):
            first, _ = await admit(client, core, config, plan, 1, card=URL)
            await select_read(core)
            if stage != "queued":
                item = await lease(client)
                result, _ = await finish(client, core, item,
                                         status="SUCCESS" if stage == "done" else stage)
                assert result["state"] == ("DONE" if stage == "done" else stage)
            before = core.store.rows("SELECT * FROM exploration_requests")
            latest, accepted = await admit(client, core, config, plan, 2, "继续读一下")
            await select_read(core)
            inherited = followup_item(
                core.store, core.settings, latest["event_id"], clock=core.clock)
            assert inherited["owner_event_id"] == first["event_id"]
            assert core.store.rows("SELECT * FROM exploration_requests") == before
            if stage == "queued":
                item = await lease(client)
                assert item["request_id"] == before[0]["request_id"]
                assert (await validate(client, item)).status_code == 200
                assert core.content.status(latest["event_id"])["pending"]
            else:
                assert await lease(client) is None
                context = json.loads(core.content.context_message(latest["event_id"], 6000))
                row = context["shared_content"][0]
                assert row["status"] == ("FETCHED" if stage == "done" else stage)
                if stage == "done":
                    assert "第一行资料" in row["text"]
                else:
                    assert "text" not in row
                assert not core.content.status(latest["event_id"])["pending"]
            assert core.store.one('SELECT attempts FROM jobs WHERE job_id=%s',
                                  (accepted["job_id"],))["attempts"] == (
                0 if stage == "queued" else 1)
            assert core.store.one("SELECT COUNT(*) n FROM model_usage")["n"] == (
                2 if stage == "queued" else 3)
    run(scenario())


@pytest.mark.parametrize(("text", "reference", "inherits"), [
    ("先聊别的", None, False), ("不用继续看了", None, False),
    ("引用里的文字不能授权", 1, True), ("引用里的文字不能授权", 999, False),
    ("暂停主动分享", None, False), ("普通新消息", None, False),
], ids=["other_topic", "stop", "local_reference", "foreign_reference", "control", "ordinary"])
def test_followup_is_explicit_and_locally_bound(
    core, tmp_path, monkeypatch, text, reference, inherits,
):
    async def scenario():
        async with window(core, tmp_path, monkeypatch) as (client, config, plan):
            first, _ = await admit(client, core, config, plan, 1, card=URL)
            await select_read(core)
            item = await lease(client)
            latest, _ = await admit(client, core, config, plan, 2, text, reference=reference)
            if inherits:
                await select_read(core)
            selected = followup_item(
                core.store, core.settings, latest["event_id"], clock=core.clock)
            assert bool(selected) is inherits, {
                "source_ids": core.store.rows("SELECT source_event_id FROM events "
                                              "WHERE source='qq.onebot'"),
                "references": core.store.rows('SELECT * FROM runtime_meta WHERE key LIKE %s',
                                               (FOLLOWUP + "%",)),
            }
            checked = await validate(client, item)
            assert checked.status_code == 200  # A superseded reply does not recall its source.
            if inherits:
                assert selected["owner_event_id"] == first["event_id"]
    run(scenario())


@pytest.mark.parametrize("change", ["original_recall", "consumer_recall", "new_turn", "epoch",
                                    "expired", "strict_scope"])
def test_followup_never_overrides_revocation(core, tmp_path, monkeypatch, change):
    async def scenario():
        async with window(core, tmp_path, monkeypatch) as (client, config, plan):
            first, _ = await admit(client, core, config, plan, 1, card=URL)
            await select_read(core)
            item = await lease(client)
            latest, _ = await admit(client, core, config, plan, 2, "你再试试看")
            await select_read(core)
            assert (await validate(client, item)).status_code == 200
            if change.endswith("recall"):
                response = await client.post(
                    "/internal/v1/owner-authority/inputs/recall", json=notice(
                    config, plan, core.clock.now, number=1 if change == "original_recall" else 2,
                ))
                assert response.status_code == 200, response.text
            elif change == "new_turn":
                await admit(client, core, config, plan, 3, "先聊别的")
            elif change == "epoch":
                with core.store.transaction():
                    advance_epoch(core.store)
            elif change == "expired":
                core.clock.advance(31)
            else:
                owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                    operation_id="stop-native-reader", authority=plan.authority))
            checked = await validate(client, item)
            # A new reply revision preserves the read. Explicit recall still
            # advances the original Reader privacy epoch and fences its lease.
            source_retained = change == "new_turn"
            assert (checked.status_code == 200) is source_retained
            result, _ = await finish(client, core, item)
            assert result["state"] == ("DONE" if source_retained else "CANCELLED")
            assert bool(result["event_ids"]) is source_retained
            assert bool(core.store.one("SELECT 1 FROM observations")) is source_retained
            assert core.store.one("SELECT used_requests,used_bytes FROM exploration_requests") == {
                "used_requests": 1, "used_bytes": 200,
            }
            if change == "original_recall":
                assert not core.store.one('SELECT redacted FROM events WHERE event_id=%s',
                                          (latest["event_id"],))["redacted"]
                assert core.store.one('SELECT redacted FROM events WHERE event_id=%s',
                                      (first["event_id"],))["redacted"]
    run(scenario())


def test_erasure_replay_removes_followup_authority_without_erasing_new_input(
    core, tmp_path, monkeypatch,
):
    async def scenario():
        async with window(core, tmp_path, monkeypatch) as (client, config, plan):
            first, _ = await admit(client, core, config, plan, 1, card=URL)
            await select_read(core)
            await lease(client)
            latest, _ = await admit(client, core, config, plan, 2, "继续看看")
            await select_read(core)
            archive, destination = tmp_path / "followup.enc", tmp_path / "restored.db"
            key = Fernet.generate_key()
            backup(core.settings.database, archive, key)
            response = await client.post(
                    "/internal/v1/owner-authority/inputs/recall", json=notice(
                config, plan, core.clock.now, number=1,
            ))
            assert response.status_code == 200, response.text
            restore(archive, destination, key, core.settings.erasure_ledger)
            with Core(core.settings.model_copy(update={"database": str(destination)}),
                      clock=core.clock) as restored:
                assert followup_item(restored.store, restored.settings, latest["event_id"],
                                     clock=restored.clock) is None
                assert not restored.store.one('SELECT 1 FROM runtime_meta WHERE key=%s',
                                              (FOLLOWUP + latest["event_id"],))
                assert restored.store.one('SELECT redacted FROM events WHERE event_id=%s',
                                          (first["event_id"],))["redacted"]
                assert not restored.store.one('SELECT redacted FROM events WHERE event_id=%s',
                                              (latest["event_id"],))["redacted"]
                assert restored.store.one("SELECT context_json FROM reading_attempts") == {
                    "context_json": "{}",
                }
                assert not restored.store.one("SELECT 1 FROM observations")
                assert all(row["phase"] == "STOPPED" and row["conversation_json"] is None
                           and row["snapshots_json"] == "[]" for row in restored.store.rows(
                               "SELECT * FROM agent_conversations"))
                assert not restored.store.one("SELECT 1 FROM agent_tool_calls "
                                               "WHERE metadata_json IS NOT NULL "
                                               "OR result_json IS NOT NULL")
    run(scenario())

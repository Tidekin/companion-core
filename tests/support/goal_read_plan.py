"""Signed HTTP inputs and real goal/Reader boundaries with synthetic transport."""

import copy
import os
import ssl
from contextlib import asynccontextmanager
from datetime import datetime

import httpcore
import httpx
from pydantic import SecretStr

from companion.config import ObservationAdapterConfig
from companion.core import Core
from companion.model import ModelClient
from source_reader import worker_v1
from source_reader.bilibili import BilibiliReader
from source_reader.network import PublicHTTP
from tests.support.core_http import real_core_http
from tests.support.heybox_reader import mock_pool
from tests.support.owner_memory import environment
from tests.support.rem_native import response
from tests.support.source_reader import META, PLAYER, SUBTITLE, RecordingStream, Responses
from tests.support.source_reader import response as network_response

TOKEN = "synthetic-goal-reader-token-" * 3
URLS = ["https://www.bilibili.com/video/BV1xx411c7m" + suffix for suffix in "DEF"]


@asynccontextmanager
async def prepared(core, tmp_path):
    cfg = core.settings
    cfg.goals_enabled = cfg.exploration_enabled = cfg.observation_enabled = True
    cfg.content_enabled = True
    cfg.observation_adapters = {"bili": ObservationAdapterConfig(
        source="bilibili", token=TOKEN, reader_max_requests=6)}
    cfg.exploration_daily_requests = cfg.exploration_monthly_requests = 40
    cfg.exploration_daily_bytes = 16 * 1024 * 1024
    cfg.control_token = SecretStr("synthetic-goal-control-token")
    async with environment(core, tmp_path, history=False,
        additional_purposes=("goals", "content", "exploration")) as (admit, _, reference):
        source, accepted = await admit(1, "准备对比几篇恒星配色资料")
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: response("合成确认"))) as model_http:
            with Core(cfg, ModelClient(cfg, model_http), core.clock) as worker:
                assert await worker.run_once(job_id=accepted["job_id"])
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer synthetic-goal-control-token"},
        ) as client:
            yield client, source.event_id, reference


async def create(client, source, *, count=3):
    result = await client.post("/internal/v2/goals", json={"topic": "比较恒星资料",
        "source_event_ids": [source], "scope": {"operation": "read_plan", "urls": URLS[:count]}})
    assert result.status_code == 202, result.text
    goal_id = result.json()["goal_id"]
    accepted = await client.post(f"/internal/v2/goals/{goal_id}/decision",
                                 json={"decision": "ACCEPT"})
    assert accepted.status_code == 200, accepted.text
    return goal_id


async def read(client, now, url, *, crash=False):
    meta = copy.deepcopy(META)
    meta["data"]["bvid"] = url.rsplit("/", 1)[-1]
    pool, backend = mock_pool(network_response(meta), network_response(PLAYER),
                              network_response(SUBTITLE))
    if crash:
        class ExitStream(RecordingStream):
            async def write(self, buffer, timeout=None):  # noqa: ASYNC109
                await super().write(buffer, timeout)
                if buffer:
                    print("provider-dispatched", flush=True)
                    os._exit(23)

        class ExitBackend(Responses):
            async def connect_tcp(self, host, port, **kwargs):
                return ExitStream(self.rows.pop(0), self)

        backend = ExitBackend(network_response(meta))
        pool = httpcore.AsyncConnectionPool(ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
                                            network_backend=backend, max_keepalive_connections=0)
    async with PublicHTTP(pool) as public, httpx.AsyncClient(
        base_url=str(client.base_url), trust_env=False,
        headers={"Authorization": "Bearer " + TOKEN},
    ) as reader_http:
        worked = await worker_v1.tick(reader_http, BilibiliReader(public, clock=lambda: now),
            "bili", source="bilibili", clock=lambda: datetime.fromisoformat(now))
    return worked, len(backend.requests)

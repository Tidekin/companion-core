"""The real worker/HTTPCore request boundary rechecks public card read authority."""

import ssl

import httpcore
import httpx
import pytest

from companion import owner_authority
from companion.owner_authority_protocol import AuthorityStop
from source_reader.heybox import HeyboxReader
from source_reader.network import PublicHTTP, PublicNetwork
from source_reader.worker_v1 import tick
from tests.conftest import run
from tests.support.source_reader import RecordingStream, Responses, response
from tests.test_live_card_content import TOKEN, captured


@pytest.mark.parametrize("stop_at", [None, "after_lease", "dns", "tls"])
def test_public_card_worker_rechecks_core_after_network_wait_before_get(core, tmp_path, stop_at):
    async def scenario():
        async with captured(core, tmp_path) as (window, core_client, _, _):
            usage = core.store.rows("SELECT * FROM model_usage")
            def stop():
                owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                    operation_id="stop-public-preflight", authority=window.runtime.authority))

            class Stream(RecordingStream):
                async def start_tls(self, *args, **kwargs):
                    if stop_at == "tls":
                        stop()
                    return await super().start_tls(*args, **kwargs)

            class Backend(Responses):
                async def connect_tcp(self, host, port, **kwargs):
                    self.hosts.append(host)
                    return Stream(self.rows.pop(0), self)

            async def resolver(host, port):
                assert (host, port) == ("www.xiaoheihe.cn", 443)
                if stop_at == "dns":
                    stop()
                return ["1.1.1.1"]

            validations = []

            async def after_core(response):
                await response.aread()
                if response.request.url.path.endswith("/lease") and stop_at == "after_lease":
                    stop()
                if response.request.url.path.endswith("/validate"):
                    validations.append(response.status_code)

            backend = Backend(response(
                "<title>公开耳机评测</title><article><p>作者比较两款耳机的佩戴与隔音体验。</p></article>"
                .encode(),
            ))
            pool = httpcore.AsyncConnectionPool(
                ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
                network_backend=PublicNetwork(resolver, backend), max_keepalive_connections=0,
            )
            async with PublicHTTP(pool) as public, httpx.AsyncClient(
                base_url=core_client.base_url,
                headers={"Authorization": "Bearer " + TOKEN},
                event_hooks={"response": [after_core]},
            ) as client:
                if stop_at:
                    assert await tick(client, HeyboxReader(public, clock=core.clock), "reader",
                                      source="heybox", clock=lambda: core.clock.now)
                    assert validations and validations[-1] != 200
                    assert all(status == 200 for status in validations[:-1])
                    assert backend.requests == []
                    assert not core.store.one("SELECT 1 FROM observations")
                else:
                    assert await tick(client, HeyboxReader(public, clock=core.clock), "reader",
                                   source="heybox", clock=lambda: core.clock.now)
                    assert validations and all(status == 200 for status in validations)
                    assert len(backend.requests) == 1
                    saved = core.store.one("SELECT state FROM exploration_requests")
                    assert saved["state"] == "DONE"
                # Connections go through the existing public DNS pinning backend.
                assert backend.hosts == ([] if stop_at == "after_lease" else ["1.1.1.1"])
                assert core.store.rows("SELECT * FROM model_usage") == usage

    run(scenario())

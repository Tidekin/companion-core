from dataclasses import dataclass

import httpx
import pytest
from pydantic import SecretStr

from companion import gateway
from companion.gateway_contract import ChatRequest, ClientGrant, RequestContext
from companion.model import ModelClient
from companion.store import after
from tests.conftest import run
from tests.support.paid_model import approved_live


@dataclass
class Harness:
    core: object
    config_path: object
    grant: ClientGrant
    key: str

    def submit(self, *, text="hello", model="xiaofeiyuv3", key="same-request-123", **options):
        body = ChatRequest(model=model, messages=[{"role": "user", "content": text}], **options)
        return gateway.submit(self.core.store, self.core.settings, self.grant, body,
                              RequestContext(), key, self.core.clock())

    def process(self, handler):
        async def work():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
                model = ModelClient(self.core.settings, transport)
                return await gateway.run_once(self.core.store, self.core.settings, model,
                                              self.core.clock)
        return run(work())


@pytest.fixture(name="harness")
def gateway_harness(core, tmp_path, monkeypatch):
    approved_live(core, tmp_path)
    core.settings.control_token = SecretStr("synthetic-control-credential-" * 2)
    path = tmp_path / "gateway.toml"
    path.write_text('enabled = true\nwait_seconds = 0\n' + ''.join(
        f'[[aliases]]\nmodel_alias = "xiaofeiyuv{i}"\nroute_id = "approved_default"\nrevision = 1\n'
        for i in (1, 2, 3)), encoding="utf-8")
    monkeypatch.setenv("COMPANION_GATEWAY_CONFIG", str(path))
    reference = gateway.budget_reference(core.store, core.settings, core.clock())
    grant = ClientGrant.model_validate({
        "client_id": "client-a", "client_kind": "api", "key_id": "key-a-1",
        "grant_id": "client-a-grant", "revision": 1,
        "expires_at": after(core.clock(), 7200), "default_mode": "passthrough",
        "modes": [{"mode": "passthrough"}],
        "model_aliases": ["xiaofeiyuv1", "xiaofeiyuv2", "xiaofeiyuv3"],
        "budget": {**reference, "max_input_tokens": 4000, "max_output_tokens": 128,
                    "daily_calls": 5,
                    "concurrency": 1},
    })
    key = gateway.put_client(core.store, core.settings, grant, core.clock())
    return Harness(core, path, grant, key)


def body(model="xiaofeiyuv3", **changes):
    return {"model": model, "messages": [{"role": "user", "content": "hello"}], **changes}


def rotate(harness, **changes):
    payload = harness.grant.model_dump(mode="json")
    payload.update(revision=harness.grant.revision + 1,
                   key_id="key-a-" + str(harness.grant.revision + 1), **changes)
    harness.grant = ClientGrant.model_validate(payload)
    harness.key = gateway.put_client(harness.core.store, harness.core.settings, harness.grant,
                                     harness.core.clock())

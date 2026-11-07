"""Real signed v2 HTTP inputs with explicit independent memory/history scopes."""

import hashlib
from contextlib import asynccontextmanager
from datetime import datetime

import httpx

from companion import owner_authority
from companion.owner_authority_protocol import OwnerCapture, OwnerGrantPolicy
from companion.qq_rich import input_digest
from companion.store import after
from companion_delivery_protocol.authority import capture_signing_payload
from tests.support.core_http import real_core_http
from tests.support.owner_authority import activate
from tests.support.owner_input import captured, inbound
from tests.support.owner_recall import receipt


@asynccontextmanager
async def environment(core, tmp_path, *, history=True, additional_purposes=(),
                      capability_policies=None):
    original, key = captured(core, tmp_path)
    core.settings.memory_enabled = True
    core.settings.extraction_idle_seconds = 0
    core.settings.context_soft_tokens = 12000
    view = owner_authority.current(core.store, core.settings, core.clock)
    policy = OwnerGrantPolicy.model_validate({**view.policy.model_dump(),
        "purposes": ["chat", "memory", *(["history"] if history else []), *additional_purposes],
        "capability_policies": capability_policies or {},
        "memory_scope": "owner_private", "history": {
            "scope": "owner_private", "not_before": after(core.clock(), -1),
            "max_turns": 15, "max_utf8_bytes": 16000} if history else {}})
    reference = activate(core, policy, revision=1, operation="memory-context-authority")[0][
        "authority"]
    async with real_core_http(core) as port, httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}", trust_env=False,
        headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
    ) as client:
        async def admit(number, text, *, ingest_event=True):
            source = original.source_event_id.rsplit(":", 1)[0] + ":" + str(number)
            value = original.model_dump(mode="json", exclude={"signature"}) | {
                "authority": reference, "source_event_id": source,
                "event_id": "evt_" + hashlib.sha256(source.encode()).hexdigest(),
                "input_sha256": input_digest({"text": text}),
                "received_at": core.clock(), "occurred_at": core.clock()}
            value["signature"] = key.sign(capture_signing_payload(value)).hex()
            capture = OwnerCapture.model_validate(value)
            accepted = await client.post("/internal/v1/owner-authority/inputs/approve",
                                         json=capture.model_dump(mode="json"))
            assert accepted.status_code == 200, accepted.text
            if not ingest_event:
                return capture, accepted.json()
            message = inbound(core, capture)
            message = message.model_copy(update={
                "occurred_at": datetime.fromisoformat(core.clock()),
                "payload": message.payload.model_copy(update={"text": text})})
            accepted = await client.post("/internal/v1/inbox/events",
                json=message.model_dump(mode="json"),
                headers={"X-Idempotency-Key": message.event_id})
            assert accepted.status_code == 202, accepted.text
            core.clock.advance(1)
            return capture, accepted.json()

        async def recall(capture):
            return await client.post("/internal/v1/owner-authority/inputs/recall",
                json=receipt(core, capture, key).model_dump(mode="json"))
        yield admit, recall, reference

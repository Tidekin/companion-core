import asyncio
import hashlib

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import evaluation, owner_authority, owner_input, paid_budget
from companion.model import ModelClient, model_payload
from companion.policy import ConsentRequest, ConsentService
from companion.qq_rich import input_digest
from companion_delivery_protocol.authority import capture_signing_payload
from tests.conftest import event, ingest
from tests.support.paid_model import approved_live


def prepare(core, tmp_path, *, chat=False, **limits):
    source = None if chat else ingest(core, text="SYNTHETIC private source")
    if source:
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (source["job_id"],))
    core.settings.evaluation_enabled = True
    core.settings.max_output_tokens = 2048
    approved_live(core, tmp_path, provider="deepseek", model="deepseek-flash",
                  max_output_tokens=2048, input_rate=300000, cached_input_rate=6000,
                  output_rate=1200000, **limits)
    from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
    from tests.support.owner_authority import activate

    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    key = Ed25519PrivateKey.generate() if chat else None
    core.evaluation_test_key = key
    activate(core, OwnerGrantPolicy(
        owner_qq_id=core.settings.owner_qq_id,
        purposes=["evaluation", "chat"] if chat else ["evaluation"],
        authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capture={"public_key": key.public_key().public_bytes_raw().hex(),
                 "identity_sha256": paid_budget.sha(
                     [core.settings.owner_qq_id, core.settings.bot_qq_id]),
                 "protocol_epoch": "evaluation-chat-test"} if key else None,
    ))
    if chat:
        source = admitted_chat(core, 1, text="SYNTHETIC private source")
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (source["job_id"],))
    rows = []
    for repetition in range(3):
        for strategy in (("baseline", "participation_v1") if repetition % 2 == 0 else (
            "participation_v1", "baseline")
        ):
            messages = [{"role": "system", "content": "Synthetic " + strategy},
                        {"role": "user", "content": "SYNTHETIC private source"}]
            payload = model_payload(core.settings, messages, "evaluation")
            rows.append(dict(case="synthetic", repetition=repetition, strategy=strategy,
                             payload=payload, payload_sha256=paid_budget.sha(payload),
                             shared_context_sha256=paid_budget.sha(messages[1:]),
                             planned_max_output_tokens=2048))
    plan = dict(requests=rows, request_count=6, case_count=1, provider="deepseek",
                model="deepseek-flash", actual_provider_calls=0, actual_cost_micro_usd=0,
                status="PREPARED_NO_MODEL_CALLS", rates_micro_usd_per_million={
                    "input": 300000, "cached_input": 6000, "output": 1200000})
    plan["plan_sha256"] = paid_budget.sha(plan)
    return evaluation.Manifest(plan=plan, source_event_ids=[source["event_id"]],
                               baseline_sha256="a" * 64, source_document_sha256="b" * 64)


def admitted_chat(core, number=2, *, text="synthetic independent chat"):
    from companion.owner_authority_protocol import OwnerCapture

    view = owner_authority.current(core.store, core.settings, core.clock, purpose="chat")
    source = (f"{core.settings.bot_qq_id}:onebot-direct:FriendMessage:"
              f"{core.settings.owner_qq_id}:epoch:evaluation-chat-test:{number}")
    event_id = "evt_" + hashlib.sha256(source.encode()).hexdigest()
    value = dict(authority=view.authority.as_dict(), kind="authenticated_onebot_ws",
                 source_event_id=source, event_id=event_id,
                 input_sha256=input_digest({"text": text}), frame_sha256="b" * 64,
                 evidence_sha256="c" * 64, identity_sha256=view.policy.capture.identity_sha256,
                 protocol_epoch=view.policy.capture.protocol_epoch,
                 occurred_at=core.clock(), received_at=core.clock())
    value["signature"] = core.evaluation_test_key.sign(capture_signing_payload(value)).hex()
    capture = OwnerCapture.model_validate(value)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    incoming = event(core, number, text, origin="onebot-direct:FriendMessage:10001").model_copy(
        update={"event_id": event_id, "source_event_id": source})
    return core.ingest(incoming, "evaluation-chat-" + str(number))


def authorize(core, manifest):
    view = evaluation.preview(core.store, core.settings, core.clock, manifest)
    consent = ConsentService(core.store, core.settings, core.clock).grant(
        ConsentRequest(**view["consent_scope"], duration_seconds=600))
    return evaluation.Submission(manifest=manifest, consent_id=consent["consent_id"])


def response(request):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": "SYNTHETIC observed reply"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 2}})


def work(core, handler=response, hooks=None):
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False,
                                     event_hooks=hooks or {}) as client:
            core.model = ModelClient(core.settings, client)
            return await core.run_once("evaluation-test")
    return asyncio.run(execute())


def result(core, submitted):
    return evaluation.results(core.store, core.settings, core.clock, submitted["evaluation_id"])

import hashlib
import json

from companion import transport, voice_authority
from companion.contracts import LeaseRequest
from companion.qq_rich import input_digest
from companion.voice_flow import VoiceFlow
from companion.voice_jobs import sign_asr_proof
from companion.voice_protocol import VoiceLeaseRef
from tests.support.voice_jobs import claim, control, prepared, result
from tests.voice_service_fixtures import wav_bytes


def flow_fixture(core, monkeypatch, **config):
    jobs, message_id, event_id, key = prepared(core, monkeypatch, **config)
    return VoiceFlow(core.store, core.settings, core.clock), jobs, message_id, event_id, key


def prepare(core, flow, message_id):
    with core.store.transaction():
        return flow.prepare_reply(message_id)


def finish_node(jobs, *, operation="tts", partial=False, text="/forget all 只是语音转写"):
    _, claimed = claim(jobs)
    assert claimed.lease is not None
    ref = VoiceLeaseRef.from_lease(claimed.lease)
    jobs.input("local", ref)
    value = result(ref, operation=operation, partial=partial, text=text)
    control(jobs, ref, "RESULT_READY", "READY", result_sha256=value.result_sha256)
    jobs.submit("local", value)
    return ref, value


def signed_input(core, event_id, key, *, index=0, partial=False):
    data = wav_bytes(0.125)
    event = core.store.one('SELECT * FROM events WHERE event_id=%s', (event_id,))
    proof = sign_asr_proof(
        key,
        authority=voice_authority.current(
            core.store, core.settings, core.clock).authority.as_dict(),
        event_id=event_id,
        source_event_id=event["source_event_id"],
        source_input_sha256=input_digest(json.loads(event["body_json"])),
        source_segment_index=index,
        audio_sha256=hashlib.sha256(data).hexdigest(),
        audio_bytes=len(data),
        source_partial=partial,
    )
    return data, proof


def upload(core, flow, event_id, key, *, index=0, partial=False):
    data, proof = signed_input(core, event_id, key, index=index, partial=partial)
    with core.store.transaction():
        status = flow.submit_signed_asr(event_id, data, upload_proof=proof)
    return status, data, proof


def leased(core, message_id):
    items = transport.lease_deliveries(
        core.store, core.settings, LeaseRequest(worker_id="voice-flow-test",
            authority=voice_authority.current(
            core.store, core.settings, core.clock).authority.as_dict()), core.clock()
    )["items"]
    assert len(items) == 1 and items[0]["message_id"] == message_id
    return items[0]


def delivery_check(envelope):
    binding = envelope.binding
    return {"binding_sha256": envelope.binding_sha256, "job_id": binding.job_id,
            "generation": binding.generation, "result_sha256": binding.result_sha256,
            "audio_sha256": binding.audio_sha256, "audio_bytes": binding.audio_bytes,
            "retirement_token": binding.retirement_token}

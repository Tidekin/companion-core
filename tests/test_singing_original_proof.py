import copy
import json

import pytest

from companion import voice_protocol
from companion.voice_original_proof import validate_original_voice_binding
from companion.voice_protocol import VoiceDeliveryBinding
from tests.test_singing_protocol import result
from tests.test_voice_original_proof import original


def records(*, retired=False, index=0):
    value = original(retired=retired)
    job, outbox = value["job"], value["outbox"]
    job.update(operation="sing", message_id=None)
    outbox.update(origin_event_id=job["event_id"], segment_index=index, segment_count=2)
    if not retired:
        song = result()
        manifest = {**song.manifest, "node_manifest_sha256": "c" * 64}
        song = voice_protocol.SingingResult(manifest=manifest, audio=song.audio)
        binding = value["receipt"]["binding"] | {
            "singing_segment_index": index, "result_sha256": voice_protocol.digest(manifest),
            "audio_sha256": song.audio[index].audio_sha256,
            "audio_bytes": len(song.audio[index].audio_bytes())}
        binding = VoiceDeliveryBinding.model_validate(binding).model_dump(mode="json")
        value["receipt"].update(binding=binding, binding_sha256=voice_protocol.digest(binding))
        given = {**value["proof"]["voice_check"], "binding_sha256": voice_protocol.digest(binding),
            "result_sha256": binding["result_sha256"], "audio_sha256": binding["audio_sha256"],
            "audio_bytes": binding["audio_bytes"]}
        value["proof"]["voice_check"] = given
        job.update(result_sha256=binding["result_sha256"],
                   result_blob=voice_protocol.canonical_bytes(song),
                   outbox_content_sha256=None, outbox_delivery_key=None)
    return value


@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("index", [0, 1])
def test_original_song_part_remains_provable_after_expiry_and_retirement(retired, index):
    evidence = records(retired=retired, index=index)
    before = copy.deepcopy(evidence)
    validate_original_voice_binding(**evidence)
    assert evidence == before


@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("index", [0, 1])
def test_mixed_song_original_proof_uses_song_index_and_retains_retirement(retired, index):
    evidence = records(retired=retired, index=index)
    outbox = evidence["outbox"]
    outbox.update(segment_index=index + 1, segment_count=4,
                  delivery_key=f"reply:original-event:v2:{index + 1}")
    if retired:
        evidence["receipt"]["retired"]["delivery_key"] = outbox["delivery_key"]
    else:
        content = {"text": "original reply", "reply_part": {"kind": "singing",
            "source_index": 1, "request_id": evidence["job"]["job_id"],
            "singing_segment_index": index}}
        outbox["content_json"] = json.dumps(content)
        evidence["content_sha256"] = voice_protocol.digest(content)
        binding = evidence["receipt"]["binding"]
        binding.update(delivery_key=outbox["delivery_key"],
                       content_sha256=evidence["content_sha256"])
        evidence["receipt"]["binding_sha256"] = voice_protocol.digest(binding)
        evidence["proof"]["voice_check"]["binding_sha256"] = voice_protocol.digest(binding)
    before = copy.deepcopy(evidence)
    validate_original_voice_binding(**evidence)
    assert evidence == before


@pytest.mark.parametrize("change", ["index", "audio", "node", "job", "scope", "partition"])
def test_original_song_proof_rejects_substitution(change):
    evidence = records()
    if change == "index":
        evidence["outbox"]["segment_index"] = 1
    elif change == "audio":
        evidence["proof"]["voice_check"]["audio_sha256"] = "0" * 64
    elif change == "node":
        evidence["job"]["manifest_sha256"] = "0" * 64
    elif change == "job":
        evidence["job"]["message_id"] = evidence["outbox"]["message_id"]
    elif change == "scope":
        evidence["original_scope"]["authority"]["revision"] += 1
    else:
        evidence["outbox"]["segment_count"] += 1
    with pytest.raises(ValueError):
        validate_original_voice_binding(**evidence)

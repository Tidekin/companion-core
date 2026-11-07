import copy
import hashlib
import json

import pytest

from companion.voice_original_proof import validate_original_voice_binding
from tests.support.voice_original_proof import original


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("retired", [False, True])
def test_original_custody_survives_expiry_and_retirement_without_live_authority(legacy, retired):
    records = original(legacy=legacy, retired=retired)
    before = copy.deepcopy(records)
    validate_original_voice_binding(**records)
    assert records == before


@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("change", ["authority", "policy", "job", "token", "missing"])
def test_original_custody_rejects_changed_evidence(retired, change):
    records = original(retired=retired)
    if change == "authority":
        records["original_scope"] = copy.deepcopy(records["original_scope"])
        records["original_scope"]["authority"]["revision"] = 2
    elif change == "policy":
        records["voice_policy_sha256"] = "0" * 64
    elif change == "job":
        records["job"]["generation"] = 2
    elif change == "token":
        records["proof"]["voice_check"]["retirement_token"] = "s" * 32
    else:
        records["proof"].pop("voice_check")
    with pytest.raises(ValueError):
        validate_original_voice_binding(**records)


@pytest.mark.parametrize("change", ["content", "bytes", "result", "expiry", "job_authority"])
def test_full_binding_rejects_changed_original_material(change):
    records = original()
    if change == "content":
        records["outbox"]["content_json"] = json.dumps({"text": "changed"})
    elif change == "bytes":
        records["proof"]["voice_check"]["audio_bytes"] = 45
    elif change == "result":
        records["job"]["result_sha256"] = "0" * 64
    elif change == "expiry":
        records["job"]["result_expires_at"] = "2001-01-01T00:00:00Z"
    else:
        records["job"]["authority_json"] = "{}"
    with pytest.raises(ValueError):
        validate_original_voice_binding(**records)


def test_legacy_signature_retirement_is_original_signature_bound():
    records = original(legacy=True, retired=True)
    records["proof"]["voice_check"].pop("retirement_token")
    records["proof"]["signature"] = "ab" * 64
    records["receipt"]["retired"].update(
        kind="legacy_signature", verifier=hashlib.sha256(bytes.fromhex("ab" * 64)).hexdigest())
    validate_original_voice_binding(**records)
    records["proof"]["signature"] = "cd" * 64
    with pytest.raises(ValueError, match="retired voice proof changed"):
        validate_original_voice_binding(**records)

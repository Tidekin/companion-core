import hashlib
import json

import pytest

from companion import qq_files
from companion.contracts import CoreError
from companion.store import encode
from tests.support.qq_files import prepared, signed_upload


def test_file_source_requires_real_capture_and_current_owner(core, monkeypatch):
    _, approval, _, event_id, files = prepared(core, monkeypatch)
    assert qq_files.source(core.store, core.settings, approval, event_id)[2] == files
    receipt = core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                             ("live_text_input:" + event_id,))
    broken = json.loads(receipt["value"])
    broken["capture"]["signature"] = "0" * 128
    core.store.execute("UPDATE runtime_meta SET value=? WHERE key=?",
                       (encode(broken), "live_text_input:" + event_id))
    with pytest.raises(CoreError, match="LIVE_SOURCE_CAPTURE_CHANGED"):
        qq_files.source(core.store, core.settings, approval, event_id)


def test_image_permission_does_not_authorize_files(core, monkeypatch):
    _, approval, _, event_id, _ = prepared(core, monkeypatch)
    with pytest.raises(CoreError, match="PRIVATE_FILE_SCOPE_REQUIRED"):
        qq_files.source(core.store, core.settings,
                        approval.model_copy(update={"file_scope": "none"}), event_id)
    core.store.execute("UPDATE conversations SET reply_revision=reply_revision+1")
    with pytest.raises(CoreError, match="REPLY_SUPERSEDED"):
        qq_files.source(core.store, core.settings, approval, event_id)


@pytest.mark.parametrize("change", [
    {"filename": "../outside.docx"}, {"filename": " "}, {"file_id": "id\x00"},
    {"slot": True}, {"size": 0}, {"size": True}, {"source_sha256": "not-a-hash"},
])
def test_file_manifest_rejects_ambiguous_metadata(change):
    original = {"index": 0, "type": "file", "actor": "UNVERIFIED", "coverage": "UNREAD",
                "slot": 0, "file_id": "native", "filename": "a.txt", "size": None,
                "source_sha256": "a" * 64} | change
    with pytest.raises(CoreError, match="PRIVATE_FILE_MANIFEST_INVALID"):
        qq_files.manifest({"rich": {"segments": [original]},
                           "shared_content": [{"kind": "file", "slot": 0}]})


@pytest.mark.parametrize("field", ["file_id", "filename", "media_sha256", "lease_token",
                                   "source_sha256", "mime", "bytes", "slot"])
def test_upload_signature_binds_source_metadata_and_actual_bytes(core, monkeypatch, field):
    _, approval, key, event_id, files = prepared(core, monkeypatch)
    data, claim = b"payload", {"lease_token": "content_lease_" + "1" * 32}
    proof = signed_upload(approval, key, event_id, files[0], claim, data)
    assert qq_files.upload_proof(approval, event_id, files[0], claim["lease_token"],
                                 hashlib.sha256(data).hexdigest(), "text/plain", len(data), proof)
    proof[field] = 1 if field == "bytes" else True if field == "slot" else "changed"
    with pytest.raises(CoreError, match="PRIVATE_FILE_UPLOAD_SIGNATURE_REQUIRED"):
        qq_files.upload_proof(approval, event_id, files[0], claim["lease_token"],
                              hashlib.sha256(data).hexdigest(), "text/plain", len(data), proof)

"""Owner-uploaded fixtures preserve source class, native erasure and accounting."""
import copy

import pydantic
import pytest
from cryptography.fernet import Fernet

from companion import evaluation
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.runtime import Runtime
from scripts import evaluate_image_understanding as experiment
from tests.test_evaluation import authorize, result, work
from tests.test_image_evaluation import prepared


def uploaded(core, tmp_path):
    _, manifests = prepared(core, tmp_path)
    rows = [row for row in manifests[0].plan["requests"] if any(
        isinstance(message["content"], list) for message in row["payload"]["messages"])][:2]
    return evaluation.Manifest.model_validate(experiment.manifest(
        core.settings, rows, False, kind="owner_upload"))


def test_upload_kind_rejects_core_source_claims_and_requires_original_pixels(core, tmp_path):
    manifest = uploaded(core, tmp_path)
    assert manifest.model_dump()["kind"] == "owner_upload"
    with pytest.raises(pydantic.ValidationError):
        evaluation.Manifest.model_validate({**manifest.model_dump(), "source_event_ids": ["evt_x"]})
    altered = copy.deepcopy(manifest.model_dump())
    for row in altered["plan"]["requests"]:
        row["payload"]["messages"][-1]["content"] = "text only"
    # Even a freshly computed valid digest cannot replace the uploaded pixels with text.
    from tests.test_evaluation_measurement_v2 import rehash

    rehash(altered["plan"])
    with pytest.raises(CoreError, match="MANIFEST_INVALID"):
        evaluation.preview(core.store, core.settings, core.clock,
                           evaluation.Manifest.model_validate(altered))
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_uploaded_pixels_results_and_old_backup_follow_native_erasure(core, tmp_path):
    manifest = uploaded(core, tmp_path)
    submitted = evaluation.submit(core, authorize(core, manifest))
    work(core)
    assert result(core, submitted)["state"] == "SUCCEEDED"
    stored = core.store.one("SELECT manifest_json FROM evaluation_runs")["manifest_json"]
    assert '"kind":"owner_upload"' in stored and "data:image/" in stored
    before = core.store.rows("SELECT request_id,state,billed_micro_usd FROM model_usage")
    event_id = core.store.one("SELECT event_id FROM jobs WHERE job_id=?",
                              (submitted["job_id"],))["event_id"]
    assert core.store.one("SELECT source,actor_id FROM events WHERE event_id=?", (event_id,)) == {
        "source": "system.control", "actor_id": "system"}
    archive, target = tmp_path / "uploaded.bin", tmp_path / "restored.sqlite3"
    key = Fernet.generate_key()
    backup(core.settings.database, archive, key)
    with core.store.transaction():
        core.ledger.expire_events(core.store, [event_id], core.clock())
    assert core.store.one("SELECT manifest_json FROM evaluation_runs")["manifest_json"] == "{}"
    assert core.store.rows("SELECT * FROM evaluation_results") == []
    assert core.store.rows("SELECT request_id,state,billed_micro_usd FROM model_usage") == before
    restore(archive, target, key, core.settings.erasure_ledger)
    settings = core.settings.model_copy(update={"database": str(target)})
    with Runtime(settings, core.clock) as restored:
        assert restored.store.one("SELECT manifest_json FROM evaluation_runs")[
            "manifest_json"] == "{}"
        assert restored.store.rows("SELECT * FROM evaluation_results") == []
        assert restored.store.rows(
            "SELECT request_id,state,billed_micro_usd FROM model_usage") == before
    with pytest.raises(CoreError, match="ERASED"):
        result(core, submitted)

import copy
import hashlib
import json

import pytest

from companion.beta_observation import inspect_context
from companion.transport import enqueue_reply_parts
from scripts.summarize_beta_feedback import summarize
from scripts.validate_beta_feedback import validate_record
from tests.conftest import ingest
from tests.support.p12_beta_operations import bundle, feedback
from tests.support.segmented_delivery import PARTS, freeze


def sample(core, *, visible=3, states=("SENT", "SENT", "SENT")):
    job, ids = freeze(core)
    for message_id, state in zip(ids, states, strict=True):
        core.store.execute('UPDATE outbox SET state=%s,sent_at=%s WHERE message_id=%s',
                           (state, core.clock() if state == "SENT" else None, message_id))
    report = inspect_context(core.settings, "trace_test_1")
    record = feedback()
    record["phone"].update(expected_reply_count=3,
        reply_group_sha256=core.store.one(
            'SELECT reply_group_sha256 FROM outbox WHERE message_id=%s', (ids[0],),
        )["reply_group_sha256"], received=None if visible is None else visible > 0,
        visible_reply_count=visible, duplicate_check_evidence_sha256="d" * 64)
    record["trace_ref_sha256"] = report["trace_ref_sha256"]
    record["inspector_sha256"] = hashlib.sha256(json.dumps(report).encode()).hexdigest()
    return record, report, job, ids


def test_three_frozen_replies_are_not_misreported_as_duplicates(core, tmp_path):
    record, report, job, ids = sample(core)
    assert validate_record(record)["schema_valid"]
    group = report["trace"]["reply_groups"][0]
    assert group == {
        "state": "RECORDED", "job_ref_sha256": hashlib.sha256(job["job_id"].encode()).hexdigest(),
        "reply_group_sha256": record["phone"]["reply_group_sha256"],
        "expected_reply_count": 3, "outbox": {"SENT": 3}, "all_parts_sent": True,
    }
    before = list(core.store.db.iterdump())
    result = summarize(bundle(tmp_path, [(record, report)]))["groups"]["local_synthetic"]
    assert result["phone"]["visible_replies"]["sum"] == 3
    assert not result["reported_incidents"]
    assert list(core.store.db.iterdump()) == before
    serialized = json.dumps(report, ensure_ascii=False)
    for private in [*PARTS, *ids, job["job_id"], job["event_id"], "trace_test_1"]:
        assert private not in serialized


def test_inspector_checks_the_frozen_group_without_writing_or_calling_models(core):
    _, _, _, _ = sample(core)
    before = list(core.store.db.iterdump())
    calls = len(core.model.calls)
    report = inspect_context(core.settings, "trace_test_1")
    assert report["trace"]["reply_groups"][0]["expected_reply_count"] == 3
    assert list(core.store.db.iterdump()) == before
    assert len(core.model.calls) == calls == 0


@pytest.mark.parametrize("visible", [None, 0, 1, 2])
def test_partial_or_unknown_delivery_does_not_invent_full_phone_receipt(core, tmp_path, visible):
    record, report, _, _ = sample(core, visible=visible,
                                 states=("SENT", "DELIVERY_UNCERTAIN", "CANCELLED"))
    assert report["trace"]["reply_groups"][0]["all_parts_sent"] is False
    assert validate_record(record)["schema_valid"]
    phone = summarize(bundle(tmp_path, [(record, report)]))["groups"]["local_synthetic"]["phone"]
    assert phone["visible_replies"]["sum"] == visible
    assert phone["received"] == (None if visible is None else int(visible > 0))


def test_only_visible_count_beyond_frozen_expectation_requires_duplicate_incident(core):
    record, _, _, _ = sample(core, visible=4)
    assert not validate_record(record)["schema_valid"]
    record["incidents"] = ["duplicate_visible_reply"]
    assert validate_record(record)["schema_valid"]


def test_legacy_default_remains_one_reply_and_old_record_shape_is_accepted():
    record = feedback()
    record["phone"].update(received=True, visible_reply_count=2,
                           duplicate_check_evidence_sha256="d" * 64)
    assert not validate_record(record)["schema_valid"]
    record["incidents"] = ["duplicate_visible_reply"]
    assert validate_record(record)["schema_valid"]
    record["phone"]["visible_reply_count"] = 1
    record["incidents"] = []
    assert validate_record(record)["schema_valid"]


@pytest.mark.parametrize("count", [0, 4, True, 2.5, None, "3"])
def test_expected_count_is_a_bounded_integer(core, count):
    record, _, _, _ = sample(core)
    record["phone"]["expected_reply_count"] = count
    assert not validate_record(record)["schema_valid"]


@pytest.mark.parametrize("missing", ["group", "inspector", "trace"])
def test_multiple_expected_replies_require_group_and_inspector_references(core, missing):
    record, _, _, _ = sample(core)
    if missing == "group":
        record["phone"]["reply_group_sha256"] = None
    else:
        record[missing + "_sha256" if missing == "inspector" else "trace_ref_sha256"] = None
    assert not validate_record(record)["schema_valid"]


@pytest.mark.parametrize("change", ["count", "hash", "missing_group", "duplicate_group",
                                    "wrong_job", "false_all_sent"])
def test_handwritten_expectation_or_group_summary_cannot_bypass_snapshot_binding(
    core, tmp_path, change,
):
    record, report, _, _ = sample(core)
    if change == "count":
        record["phone"].update(expected_reply_count=2, visible_reply_count=2)
    elif change == "hash":
        record["phone"]["reply_group_sha256"] = "a" * 64
    elif change == "missing_group":
        report["trace"].pop("reply_groups")
    elif change == "duplicate_group":
        report["trace"]["reply_groups"] *= 2
    elif change == "wrong_job":
        report["trace"]["reply_groups"][0]["job_ref_sha256"] = "a" * 64
    else:
        report["trace"]["reply_groups"][0]["outbox"] = {"DELIVERY_UNCERTAIN": 3}
    with pytest.raises(ValueError, match="REPLY_GROUP"):
        summarize(bundle(tmp_path, [(record, report)]))


@pytest.mark.parametrize("mutation", ["redacted", "text", "count", "manifest", "missing"])
def test_redacted_or_damaged_frozen_groups_are_not_available_as_feedback_proof(
    core, tmp_path, mutation,
):
    record, _, _, ids = sample(core)
    if mutation == "redacted":
        with core.store.transaction():
            core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    elif mutation == "missing":
        core.store.execute('DELETE FROM outbox WHERE message_id=%s', (ids[2],))
    else:
        field, value = {"text": ("content_json", '{"text":"changed"}'),
                        "count": ("segment_count", 2),
                        "manifest": ("reply_group_sha256", "a" * 64)}[mutation]
        core.store.execute(f"UPDATE outbox SET {field}=? WHERE message_id=?", (value, ids[1]))
    before = list(core.store.db.iterdump())
    report = inspect_context(core.settings, "trace_test_1")
    assert report["trace"]["reply_groups"] == []
    assert list(core.store.db.iterdump()) == before
    with pytest.raises(ValueError, match="REPLY_GROUP"):
        summarize(bundle(tmp_path, [(record, report)]))


def test_another_traces_valid_group_cannot_back_this_feedback(core, tmp_path):
    record, first, _, _ = sample(core)
    ingest(core, 2, "另一个独立合成问题")
    job = core.store.one("SELECT j.*,e.conversation_id FROM jobs j JOIN events e USING(event_id) "
                         "WHERE event_id='evt_test_2'")
    enqueue_reply_parts(core.store, job, PARTS, 1500, core.clock())
    other = inspect_context(core.settings, "trace_test_2")
    report = copy.deepcopy(first)
    report["trace"]["reply_groups"] = other["trace"]["reply_groups"]
    with pytest.raises(ValueError, match="REPLY_GROUP"):
        summarize(bundle(tmp_path, [(record, report)]))


def test_multiple_expected_replies_cannot_omit_the_inspector_file(core, tmp_path):
    record, _, _, _ = sample(core)
    with pytest.raises(ValueError, match="INSPECTOR_FILE_REQUIRED"):
        summarize(bundle(tmp_path, [(record, None)]))

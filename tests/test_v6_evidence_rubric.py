"""Scoring repairs are versioned and cannot rewrite published v6 evidence."""
import hashlib
import json

import pytest

from benchmarks.memory_campaign.repair_v6_bank import hashes, repair, write


def test_repaired_bank_keeps_sources_and_applies_one_explicit_policy_to_both_cohorts(tmp_path):
    bank = tmp_path / "original"
    suite = bank / "suite-v1"
    (suite / "inputs").mkdir(parents=True)
    (suite / "review-only").mkdir()
    write(suite / "inputs/P1.jsonl", [
        {"id": "P1-C21-S1", "text": "我打算把汀原交流函寄给她。"},
        {"id": "P1-C21-S2", "text": "青岩那封还没寄。"}])
    write(suite / "questions.jsonl", [
        {"id": "P1-C03/d00", "category_id": "C03", "question": "我在芦岸坊做什么工作？"},
        {"id": "P1-C21/d00", "category_id": "C21", "question": "汀原交流函寄出了吗？",
         "source_ids": ["P1-C21-S1", "P1-C21-S2"]}])
    write(suite / "review-only/criteria.jsonl", [
        {"id": "P1-C03/d00", "required": ["纸伞修复师", "单位是芦岸坊"],
         "exact": [["纸伞修复师"], ["芦岸坊"]]},
        {"id": "P1-C21/d00", "required": ["仍未寄出"]}])
    before = hashes(bank)
    result = repair(bank, tmp_path / "corrected")
    assert hashes(bank) == before and result["scorable_questions"] == 1
    assert result["excluded"][0]["reason"] == "UNSUPPORTED_ENTITY_COREFERENCE"
    assert result["edits"][0]["after"]["required"] == ["纸伞修复师"]
    corrected = tmp_path / "corrected"
    manifest = json.loads((corrected / "bundle-manifest.json").read_text("utf-8"))
    assert all(hashlib.sha256((corrected / name).read_bytes()).hexdigest() == digest
               for name, digest in manifest["files_sha256"].items())
    with pytest.raises(ValueError):
        repair(bank, bank)
    with pytest.raises(ValueError):
        repair(bank, corrected)

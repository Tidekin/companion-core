"""The public stage record cannot upgrade offline fixtures to real acceptance."""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from tests.support.p8c_public_materials import privacy_categories

ROOT = Path(__file__).resolve().parents[1]


def materials():
    schema = json.loads((ROOT / "contracts/p13-gates.schema.json").read_text("utf-8"))
    gates = json.loads((ROOT / "docs/qq-beta/p13-gates.json").read_text("utf-8"))
    return Draft202012Validator(schema), gates


def test_p13_baseline_is_private_and_runtime_authority_is_separate():
    validator, gates = materials()
    validator.check_schema(validator.schema)
    validator.validate(gates)
    assert gates["baseline"]["main_sha"] == "eb5143500bed5395bb45f2a2cc20335da9988491"
    assert all(value is None for value in gates["runtime"]["new_authority"].values())
    assert gates["runtime"]["current_host_state"] == "NOT_RECHECKED"
    assert not gates["private_beta_ready"] and not gates["production_ready"]
    for path in (ROOT / "docs/qq-beta").glob("p13*.*"):
        if path.suffix in {".md", ".json", ".toml"}:
            assert not privacy_categories(path.read_text("utf-8")), path.name


@pytest.mark.parametrize("field,value", [
    ("provider", "LIMITED_LIVE_PASS"), ("phone", "CONFIRMED"),
    ("cross_natural_days", "OBSERVED"),
])
def test_real_acceptance_requires_fresh_references_and_nonzero_samples(field, value):
    validator, gates = materials()
    sample = copy.deepcopy(gates)
    package = sample["packages"]["A1"]
    package["evidence"][field] = value
    with pytest.raises(ValidationError):
        validator.validate(sample)
    package.update(runtime_samples=1, runtime_approval_sha256="a" * 64,
                   runtime_evidence_sha256="b" * 64)
    if field == "cross_natural_days":
        package["natural_days"] = 1
    validator.validate(sample)  # Structural validity does not authenticate these references.


def test_offline_success_cannot_create_calendar_days_or_release_status():
    validator, gates = materials()
    sample = copy.deepcopy(gates)
    sample["packages"]["D1"]["evidence"]["offline"] = "OFFLINE_PASS"
    validator.validate(sample)
    sample["packages"]["D1"]["natural_days"] = 3
    with pytest.raises(ValidationError):
        validator.validate(sample)
    sample["packages"]["D1"]["natural_days"] = None
    sample["production_ready"] = True
    with pytest.raises(ValidationError):
        validator.validate(sample)


def test_nonzero_runtime_samples_require_approval_even_without_a_pass_claim():
    validator, gates = materials()
    gates["packages"]["A1"]["runtime_samples"] = 1
    with pytest.raises(ValidationError):
        validator.validate(gates)

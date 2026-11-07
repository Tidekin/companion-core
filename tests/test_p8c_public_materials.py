"""Privacy checks cover public documentation and authored readiness materials."""

import ipaddress
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tests.support.p8c_public_materials import privacy_categories

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_PATTERNS = (
    "README.md", "docs/**/*.md", "docs/**/*.json", "contracts/p8c*.json",
    "docs/experiments/p8b1-context-v2/*",
    "tests/test_p8c*.py",
    "tests/test_p10_contracts*.py",
)
def test_public_materials_contain_only_anonymous_target_references():
    findings = []
    for path in sorted({path for pattern in PUBLIC_PATTERNS for path in ROOT.glob(pattern)}):
        if path.is_file() and path.suffix in {".md", ".json", ".py"}:
            findings.extend((path.relative_to(ROOT).as_posix(), category)
                            for category in privacy_categories(path.read_text("utf-8")))
    if findings:
        # No offending value, source line, or private local path reaches CI output.
        pytest.fail("Public-material privacy findings: " + repr(findings), pytrace=False)


def test_scanner_accepts_documentation_examples_and_reports_categories_only():
    assert not privacy_categories("Example endpoint 203.0.113.7; local fixture 127.0.0.1")
    assert privacy_categories(str(ipaddress.IPv4Address(1))) == {"literal_network_address"}
    assert privacy_categories(str(Path.home() / "private-record")) == {"private_absolute_path"}
    assert privacy_categories("sk-" + "synthetic" * 3) == {"credential_like_value"}
    assert privacy_categories("ssh_port = " + str(2**16 - 1)) == {"ssh_endpoint_details"}


@pytest.mark.parametrize("relative", [
    "README.md", "docs/guide.md", "docs/nested/guide.md",
    "docs/qq-beta/p13-next-stage.md", "docs/qq-beta/p14-gates.json",
])
def test_new_public_documents_are_scanned_without_registering_each_stage(
    tmp_path, monkeypatch, relative,
):
    document = tmp_path / relative
    document.parent.mkdir(parents=True, exist_ok=True)
    credential = "sk-" + "synthetic" * 3
    document.write_text(credential, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    with pytest.raises(pytest.fail.Exception) as failure:
        test_public_materials_contain_only_anonymous_target_references()
    assert "credential_like_value" in str(failure.value)
    assert credential not in str(failure.value)


def test_target_schema_is_generic_and_keeps_unknown_inventory():
    schema = json.loads((ROOT / "contracts/p8c-readiness.schema.json").read_text("utf-8"))
    document = json.loads((ROOT / "docs/qq-beta/p8c-readiness.json").read_text("utf-8"))
    target_schema = schema["properties"]["environment"]["properties"]["final_target"]
    assert {"private_target_ref", "target_inventory_sha256"} <= set(target_schema["required"])
    assert not {"host", "ssh_port"} & set(target_schema["properties"])
    target = document["environment"]["final_target"]
    assert target["target_inventory_sha256"] is None
    target["private_target_ref"] = "private_target_alternate"
    target["target_inventory_sha256"] = "a" * 64
    Draft202012Validator(schema).validate(document)

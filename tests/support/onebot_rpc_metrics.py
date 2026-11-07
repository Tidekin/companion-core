import json
import math

CANARY = "private-canary-token-and-message"


def records(capsys):
    output = capsys.readouterr().out
    assert not any(value in output for value in (
        CANARY, "private.invalid", "Authorization", "msg_", "/internal/",
    ))
    result = [json.loads(line) for line in output.splitlines()]
    for row in result:
        assert set(row) == {"schema_version", "evidence_type", "stage", "monotonic_ns",
                            "elapsed_ms", "error_class"}
        assert row["schema_version"] == 1 and row["evidence_type"] == "OFFLINE_FIXTURE"
        assert type(row["monotonic_ns"]) is int and row["monotonic_ns"] > 0
        assert math.isfinite(row["elapsed_ms"]) and row["elapsed_ms"] >= 0
    return result

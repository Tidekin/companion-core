import json
import sys
from pathlib import Path

from companion.config import Settings
from companion.runtime import Runtime

settings = Settings.model_validate_json(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert settings.model_url == "https://synthetic.invalid/v1"
with Runtime(settings, lambda: sys.argv[2]) as runtime:
    with runtime.store.transaction(control_write=True):
        runtime.ledger.expire_events(runtime.store, json.loads(sys.argv[3]), sys.argv[2])
    assert runtime.store.rows("SELECT * FROM evaluation_results") == []
    assert runtime.store.one("SELECT redacted FROM evaluation_runs")["redacted"] == 1

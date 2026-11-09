"""Parsing configuration must not import business workflows or HTTP adapters."""

import os
import subprocess
import sys
from pathlib import Path


def test_settings_import_has_no_business_or_http_adapter_dependencies():
    root = Path(__file__).resolve().parents[1]
    program = """
import sys
from companion.config import Settings

forbidden = {
    'companion.content', 'companion.media', 'companion.media_provider',
    'companion.budget', 'companion.live_text', 'companion.observation',
    'companion.goals', 'companion.goal_plan',
}
assert not forbidden.intersection(sys.modules), sorted(forbidden.intersection(sys.modules))
settings = Settings(owner_qq_id='10001', bot_qq_id='10002', bridge_token='synthetic-config-' * 2,
                    database='dbname=synthetic')
assert settings.content.vision.estimated_request_micro_usd == 0
assert settings.media.stt.url == ''
assert Settings.model_validate(settings.model_dump()).model_dump() == settings.model_dump()
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", program], cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)}, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr

"""Signed HTTP sources, native Reader receipts and an independent original Life worker."""


import pytest

from tests.support.life_worker_integration import check_independent_life_worker


@pytest.mark.parametrize("scenario", [
    "bug", "reading", "qzone", "qzone_revoked", "preempt", "pause", "reading_preempt", "unknown",
])
def test_real_http_source_to_independent_worker_shadow(core, tmp_path, monkeypatch, scenario):
    return check_independent_life_worker(core, tmp_path, monkeypatch, scenario)

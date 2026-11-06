"""A retired physical image cannot return through another source or an old offer."""

import pytest
from pydantic import ValidationError

from companion import meme_service
from companion.config import Settings
from companion.contracts import CoreError
from tests.conftest import run
from tests.test_meme_core_runtime import runtime_case as runtime_case


@pytest.mark.parametrize("runtime_case", ["two_sources"], indirect=True)
def test_retired_bytes_are_excluded_for_every_source_without_erasing_originals(runtime_case):
    c = runtime_case
    before = c.core.store.rows("SELECT * FROM meme_assets")
    costs = c.core.store.rows("SELECT * FROM model_usage")
    run(c.service.prepare_offer(c.job))
    assert c.retrieval.check(c.job)["offer"]["candidates"]
    c.core.settings.meme_excluded_sha256 = [c.item["asset_sha256"]]
    for asset in before:
        caption = c.core.store.one(
            "SELECT caption_job_id FROM meme_asset_sources WHERE asset_id=?",
            (asset["asset_id"],),
        )
        with pytest.raises(CoreError, match="MEME_ASSET_UNAVAILABLE"):
            c.retrieval._resolve(asset["asset_id"], caption["caption_job_id"])
    with pytest.raises(CoreError):
        c.retrieval.check(c.job)
    assert c.core.store.rows("SELECT * FROM meme_assets") == before
    assert c.core.store.rows("SELECT * FROM model_usage") == costs


@pytest.mark.parametrize("value", [["../image"], ["A" * 64], [None], ["a" * 64] * 4097])
def test_excluded_images_are_bounded_sha256_values(core, value):
    with pytest.raises(ValidationError):
        Settings.model_validate({**core.settings.model_dump(), "meme_catalog_owner": "core",
                                 "meme_excluded_sha256": value})


def test_exclusion_order_does_not_change_the_approved_policy(core):
    first = core.settings.model_copy(update={"expression_enabled": True,
                                             "meme_excluded_sha256": ["a" * 64, "b" * 64]})
    second = first.model_copy(update={"meme_excluded_sha256": ["b" * 64, "a" * 64, "a" * 64]})
    assert meme_service.policy_hash(first) == meme_service.policy_hash(second)
    assert "meme_excluded_sha256" not in core.settings.model_dump()

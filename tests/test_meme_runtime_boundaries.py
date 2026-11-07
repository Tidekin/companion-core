"""Shared transport hard limits stay subordinate to current approval and package boundary."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from companion.meme_retrieval import media
from companion_onebot_bridge.config import BridgeConfig
from tests.support.meme_asset_migration import check_entrypoint_import_isolation as isolated_import
from tests.support.next_memes_animation import canvas_gif
from tests.support.next_memes_catalog import png


@pytest.mark.parametrize(
    "module,blocked",
    [
        ("companion.meme_policy", "companion_onebot_bridge"),
        ("companion.meme_retrieval", "companion_onebot_bridge"),
        ("companion.meme_delivery", "companion_onebot_bridge"),
        ("companion_onebot_bridge.meme_transport", "companion"),
        ("companion_onebot_bridge.meme_asset_http", "companion"),
        ("companion_onebot_bridge.owner_send", "companion"),
    ],
)
def test_runtime_imports_do_not_cross_application_boundary(module, blocked):
    isolated_import(module, blocked)


def test_current_approval_limits_each_original_and_all_gif_frame_pixels():
    data = png()
    cfg = SimpleNamespace(
        paid_model=SimpleNamespace(
            meme_input=SimpleNamespace(
                max_bytes_per_image=4_000_000, max_pixels_per_image=16_000_000
            )
        )
    )
    assert media(cfg, data).data == data
    cfg.paid_model.meme_input.max_bytes_per_image = len(data) - 1
    assert media(cfg, data) is None
    cfg.paid_model.meme_input.max_bytes_per_image = 4_000_000
    cfg.paid_model.meme_input.max_pixels_per_image = 20
    assert media(cfg, data) is None
    gif = canvas_gif([(24, 24), (24, 24)])
    cfg.paid_model.meme_input.max_pixels_per_image = 24 * 24
    assert media(cfg, gif) is None
    cfg.paid_model.meme_input.max_pixels_per_image = 24 * 24 * 2
    assert media(cfg, gif).data == gif


@pytest.mark.parametrize("value", [None, False, 1, [], {}, "fallback", "CORE"])
def test_bridge_selector_refuses_unknown_or_untyped_mode(value):
    cfg = BridgeConfig(
        owner_qq_id="10001",
        bot_qq_id="20002",
        bridge_token="b" * 32,
        onebot_token="s" * 32,
        protocol_epoch="mode-boundary-test",
    )
    with pytest.raises(ValueError, match="INVALID_MEME_CATALOG_OWNER"):
        replace(cfg, meme_catalog_owner=value)

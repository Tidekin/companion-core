"""Shared synthetic exploration setup for component and process tests."""

from companion.config import ObservationAdapterConfig
from companion.exploration import ReadItem, ReadResult

URL = "https://www.bilibili.com/video/BV1xx411c7mD"


def configure(core, social=False):
    cfg = core.settings
    cfg.observation_enabled = True
    cfg.observation_adapters = {
        "bili": ObservationAdapterConfig(source="bilibili", token="bili-reader-token-" * 3),
        "heybox": ObservationAdapterConfig(source="heybox", token="heybox-reader-token-" * 3),
    }
    cfg.goals_enabled = cfg.exploration_enabled = True
    cfg.social_enabled = social
    cfg.exploration_daily_requests = 8
    cfg.exploration_monthly_requests = 30
    cfg.exploration_daily_bytes = 8 * cfg.exploration_max_response_bytes


def discover(core, key="wake-1", url=URL, title="独立游戏新消息"):
    with core.store.transaction():
        request = core.exploration.create("bili", "discover", key)
    lease = core.exploration.lease("bili")
    result = ReadResult(
        lease_token=lease["lease_token"],
        status="SUCCESS",
        used_bytes=1200,
        items=[ReadItem(title=title, url=url)],
    )
    return core.exploration.complete("bili", request, result), result


def read_goal(core):
    goal = core.store.one('SELECT * FROM goals ORDER BY record_sequence DESC LIMIT 1')
    core.goals.accept(goal["goal_id"])
    assert core.exploration.schedule_goal()
    item = core.exploration.lease("bili")
    return goal, item

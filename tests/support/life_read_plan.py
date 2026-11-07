from pydantic import SecretStr

from tests.support.goal_read_plan import TOKEN, URLS
from tests.support.life_decision_context import prepared


def prepare(core, tmp_path, *, channel=None):
    core.settings.proactive_enabled = core.settings.diary_enabled = bool(channel)
    prepared(core, tmp_path, text="可以比较这些恒星资料：\n" + "\n".join(URLS),
             additional_purposes=["diary", "proactive"] if channel else (),
             automatic_actions=[channel] if channel else ())
    core.settings.memory_enabled = True
    core.settings.life_loop.autonomy_enabled = True
    core.settings.life_loop.max_daily_decisions = 10
    core.settings.life_loop.min_interval_seconds = 60
    core.settings.observation_adapters["bili"].token = SecretStr(TOKEN)
    core.settings.observation_adapters["bili"].reader_max_requests = 6
    core.settings.exploration_daily_requests = core.settings.exploration_monthly_requests = 40

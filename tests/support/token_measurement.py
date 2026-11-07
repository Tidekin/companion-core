from companion import token_measurement as tm
from tests.support.paid_model import approved_live

MESSAGES = [{"role": "user", "content": "hello"}]


def configure(core, tmp_path, policy="prefer_exact"):
    approved_live(core, tmp_path, provider="deepseek", model="deepseek-flash",
                  endpoint="https://api.deepseek.com")
    core.settings.token_measurement = tm.TokenMeasurementConfig(
        policy=policy, provider="deepseek", endpoint="https://api.deepseek.com",
        model="deepseek-flash", renderer="deepseek-v4.1-flash-1017", revision=tm.REVISION,
    )

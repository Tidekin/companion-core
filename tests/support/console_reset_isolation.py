from pydantic import SecretStr

from companion.console_api import COOKIE

ORIGIN = "https://console.example.test"


PASSWORD = "synthetic-console-reset-" * 2


def configure(core):
    core.settings.console.enabled = True
    core.settings.console.public_origin = ORIGIN
    core.settings.control_token = SecretStr(PASSWORD)


async def login(client):
    response = await client.post("/v1/console/session", json={"password": PASSWORD},
                                 headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"],
            "Cookie": COOKIE + "=" + response.cookies[COOKIE],
            "Idempotency-Key": "console-reset-isolation"}

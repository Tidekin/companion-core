import httpx

from companion.api import create_app
from companion.api_store import ApiStore


def core_client(core, transport=None, *, bridge_config=None):
    if bridge_config is not None:
        from pydantic import SecretStr

        core.settings.bridge_token = SecretStr(bridge_config.bridge_token)
    app = create_app(core.settings)
    app.state.core = core
    app.state.api_store = ApiStore(core.database, core.settings.state_directory)
    return httpx.AsyncClient(
        base_url=bridge_config.core_url if bridge_config else "http://core",
        transport=transport or httpx.ASGITransport(app=app),
        headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
    )

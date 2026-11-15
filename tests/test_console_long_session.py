"""The original revocable owner session survives ordinary days and Core restarts."""

from fastapi.testclient import TestClient

from companion.api import create_app
from companion.console_api import COOKIE
from companion.operator_config import ConsoleConfig
from tests.support.owner_console import ORIGIN, login
from tests.support.owner_console import client as client


def test_default_long_session_survives_restart_and_logout_revokes_original(client, core):
    assert ConsoleConfig().session_seconds == 365 * 86400
    headers = login(client)
    cookie = client.cookies.get(COOKIE)
    core.clock.advance(90 * 86400)
    assert client.get('/v1/console/session').status_code == 200
    app = create_app(core.settings)
    with TestClient(app, base_url=ORIGIN, headers={'Cookie': COOKIE + '=' + cookie}) as restarted:
        app.state.core.clock = core.clock
        assert restarted.get('/v1/console/session').status_code == 200
        assert restarted.delete('/v1/console/session', headers=headers).status_code == 200
    assert client.get('/v1/console/session').status_code == 401


def test_login_cookie_uses_long_max_age_and_credential_still_revokes(client, core):
    from pydantic import SecretStr

    from tests.support.owner_console import PASSWORD

    response = client.post('/v1/console/session', json={'password': PASSWORD},
                           headers={'Origin': ORIGIN})
    assert response.status_code == 200
    cookie = response.headers['set-cookie']
    assert 'Max-Age=31536000' in cookie
    assert all(part in cookie for part in ('Secure', 'HttpOnly', 'SameSite=strict'))
    core.settings.control_token = SecretStr('rotated-owner-control-credential')
    assert client.get('/v1/console/session').status_code == 401

"""Account GETs require current Core authority as well as a local grant."""

import json
from datetime import datetime, timedelta

import httpx
import pytest

from companion.api import create_app
from companion.privacy import advance_epoch
from companion.source_grants import SourceGrantRegistration, SourceGrantSnapshot
from source_reader.bili_tools import BilibiliAccountHTTP
from source_reader.bilibili import BilibiliReader
from source_reader.heybox import HeyboxReader
from source_reader.heybox_source import NativeHeyboxSource
from source_reader.worker import tick
from tests.conftest import run
from tests.support.exploration import URL as BILI_URL
from tests.support.exploration import configure
from tests.support.source_reader import META, PLAYER, SUBTITLE, response
from tests.test_heybox_reader import URL, mock_pool
from tests.test_plugin_readonly import native_post, registered_grant


class BoundaryTransport(httpx.AsyncBaseTransport):
    def __init__(self, app, boundary=None):
        self.inner = httpx.ASGITransport(app)
        self.boundary = boundary or (lambda request, result: None)
        self.paths = []

    async def handle_async_request(self, request):
        self.paths.append(request.url.path)
        result = await self.inner.handle_async_request(request)
        await result.aread()
        replacement = self.boundary(request, result)
        return replacement if replacement is not None else result


def setup_read(core, tmp_path, source="heybox", expires_in=3600):
    configure(core)
    adapter = "heybox" if source == "heybox" else "bili"
    cfg = core.settings.observation_adapters[adapter]
    cfg.source_grant_required = True
    cfg.reader_max_requests = 1 if source == "heybox" else 3
    path = registered_grant(core, tmp_path, source)
    data = json.loads(path.read_text("utf-8"))
    if expires_in != 3600:
        data["authorization"]["epoch"] += 1
        data["expires_at"] = (
            datetime.fromisoformat(core.clock()) + timedelta(seconds=expires_in)
        ).isoformat()
        path.write_text(json.dumps(data), encoding="utf-8")
        core.observations.grants.register(
            SourceGrantRegistration(
                adapter_id=adapter, expires_at=data["expires_at"], **data["authorization"]
            )
        )
    with core.store.transaction():
        core.exploration.create(
            adapter, "read", "audit-read", URL if source == "heybox" else BILI_URL
        )
    app = create_app(core.settings)
    app.state.core = core
    return adapter, path, data, app


@pytest.mark.parametrize("change", ["revoke", "forget", "rotation", "expiry"])
@pytest.mark.parametrize("source", ["heybox", "bilibili"])
def test_core_change_after_lease_stops_first_account_get(core, tmp_path, change, source):
    adapter, path, data, app = setup_read(core, tmp_path, source, expires_in=5)
    started = datetime.fromisoformat(core.clock())

    def boundary(request, result):
        if request.url.path.endswith("/lease"):
            assert result.status_code == 200
            grant_id = data["authorization"]["grant_id"]
            if change == "revoke":
                core.observations.grants.revoke(grant_id)
            elif change == "forget":
                core.observations.grants.forget(grant_id, core.ledger)
            elif change == "rotation":
                core.observations.grants.register(
                    SourceGrantRegistration(
                        adapter_id=adapter,
                        expires_at=data["expires_at"],
                        **{**data["authorization"], "epoch": 3, "fingerprint": "f" * 64},
                    )
                )
            else:
                core.clock.advance(6)  # Core expiry, while the Reader clock still lags.

    async def scenario():
        pool, backend = mock_pool(
            *(
                [response(native_post())]
                if source == "heybox"
                else [response(META), response(PLAYER), response(SUBTITLE)]
            )
        )
        account = (
            NativeHeyboxSource(path, pool, clock=lambda: started)
            if source == "heybox"
            else BilibiliAccountHTTP(path, pool, clock=lambda: started)
        )
        async with account:
            reader = (
                HeyboxReader(None, account, clock=core.clock)
                if source == "heybox"
                else BilibiliReader(account, clock=core.clock)
            )
            async with httpx.AsyncClient(
                base_url="http://core",
                transport=BoundaryTransport(app, boundary),
                headers={
                    "Authorization": "Bearer "
                    + core.settings.observation_adapters[adapter].token.get_secret_value()
                },
            ) as client:
                with pytest.raises(httpx.HTTPStatusError):
                    await tick(client, reader, adapter)
        assert not backend.requests
        assert not core.store.rows("SELECT * FROM observations")
        assert (
            core.store.one("SELECT used_requests FROM exploration_requests")["used_requests"]
            is None
        )

    run(scenario())


@pytest.mark.parametrize(
    "failure",
    ["unauthorized", "wrong_adapter", "invalid_token", "timeout", "bad_reply", "numeric_true"],
)
def test_failed_core_revalidation_never_reaches_account_get(core, tmp_path, failure):
    adapter, path, _, app = setup_read(core, tmp_path)
    started = datetime.fromisoformat(core.clock())

    def boundary(request, result):
        if request.url.path.endswith("/validate"):
            if failure == "unauthorized":
                return httpx.Response(401, json={"error": "UNAUTHORIZED"})
            if failure == "timeout":
                raise httpx.ReadTimeout("synthetic validation timeout", request=request)
            if failure == "bad_reply":
                return httpx.Response(200, json={"valid": False})
            if failure == "numeric_true":
                return httpx.Response(200, json={"valid": 1})
        elif request.url.path.endswith("/lease"):
            item = json.loads(result.content)["item"]
            if failure == "invalid_token":
                item["lease_token"] = "invalid_lease_token"
            elif failure == "wrong_adapter":
                with core.store.transaction():
                    core.store.execute("UPDATE exploration_requests SET adapter_id='bili'")
            return httpx.Response(200, json={"item": item})

    async def scenario():
        pool, backend = mock_pool(response(native_post()))
        async with NativeHeyboxSource(path, pool, clock=lambda: started) as account:
            async with httpx.AsyncClient(
                base_url="http://core",
                transport=BoundaryTransport(app, boundary),
                headers={
                    "Authorization": "Bearer "
                    + core.settings.observation_adapters[adapter].token.get_secret_value()
                },
            ) as client:
                try:
                    await tick(client, HeyboxReader(None, account, clock=core.clock), adapter)
                except httpx.HTTPStatusError:
                    pass  # Rejected results retain the original reservation.
        assert not backend.requests
        assert not core.store.rows("SELECT * FROM observations")

    run(scenario())


@pytest.mark.parametrize("source", ["heybox", "bilibili"])
def test_local_change_during_core_validation_stops_account_get(core, tmp_path, source):
    adapter, path, data, app = setup_read(core, tmp_path, source)
    started = datetime.fromisoformat(core.clock())

    def boundary(request, result):
        if request.url.path.endswith("/validate"):
            assert result.status_code == 200
            path.write_text(json.dumps({**data, "enabled": False}), encoding="utf-8")

    async def scenario():
        pool, backend = mock_pool(
            *(
                [response(native_post())]
                if source == "heybox"
                else [response(META), response(PLAYER), response(SUBTITLE)]
            )
        )
        account = (
            NativeHeyboxSource(path, pool, clock=lambda: started)
            if source == "heybox"
            else BilibiliAccountHTTP(path, pool, clock=lambda: started)
        )
        async with account:
            reader = (
                HeyboxReader(None, account, clock=core.clock)
                if source == "heybox"
                else BilibiliReader(account, clock=core.clock)
            )
            async with httpx.AsyncClient(
                base_url="http://core",
                transport=BoundaryTransport(app, boundary),
                headers={
                    "Authorization": "Bearer "
                    + core.settings.observation_adapters[adapter].token.get_secret_value()
                },
            ) as client:
                assert await tick(client, reader, adapter)
        assert not backend.requests
        assert not core.store.rows("SELECT * FROM observations")

    run(scenario())


def test_each_bilibili_get_revalidates_and_revocation_stops_subtitles(core, tmp_path):
    adapter, path, data, app = setup_read(core, tmp_path, "bilibili")
    started = datetime.fromisoformat(core.clock())
    validations = []

    def boundary(request, result):
        if request.url.path.endswith("/validate"):
            validations.append(result.status_code)
            if len(validations) == 1:
                # Revocation after the permit cannot recall that GET; the next must stop.
                core.observations.grants.revoke(data["authorization"]["grant_id"])

    async def scenario():
        pool, backend = mock_pool(response(META), response(PLAYER), response(SUBTITLE))
        async with BilibiliAccountHTTP(path, pool, clock=lambda: started) as account:
            async with httpx.AsyncClient(
                base_url="http://core",
                transport=BoundaryTransport(app, boundary),
                headers={
                    "Authorization": "Bearer "
                    + core.settings.observation_adapters[adapter].token.get_secret_value()
                },
            ) as client:
                with pytest.raises(httpx.HTTPStatusError):
                    await tick(client, BilibiliReader(account, clock=core.clock), adapter)
        assert len(backend.requests) == 1
        assert validations == [200, 409]
        assert not core.store.rows("SELECT * FROM observations")

    run(scenario())


@pytest.mark.parametrize(
    "change,status",
    [
        ("none", 200),
        ("privacy", 409),
        ("lease_expiry", 409),
        ("revision", 409),
        ("wrong_token", 401),
        ("cross_adapter", 409),
        ("credentials", 400),
    ],
)
def test_core_validation_is_bound_to_current_lease_and_adapter(core, tmp_path, change, status):
    adapter, _, data, app = setup_read(core, tmp_path)
    snapshot = SourceGrantSnapshot(**data["authorization"], expires_at=data["expires_at"])
    item = core.exploration.lease(adapter, snapshot)
    body = {"lease_token": item["lease_token"], "source_grant": item["source_grant"]}
    if change == "privacy":
        with core.store.transaction():
            advance_epoch(core.store)
    elif change == "lease_expiry":
        core.clock.advance(31)
    elif change == "revision":
        body["source_grant"] = {**body["source_grant"], "fingerprint": "f" * 64}
    elif change == "cross_adapter":
        adapter = "bili"
    elif change == "credentials":
        body["cookies"] = {"pkey": "PRIVATE_VALIDATION_CREDENTIAL"}
    token = core.settings.observation_adapters[adapter].token.get_secret_value()
    if change == "wrong_token":
        token = core.settings.bridge_token.get_secret_value()
    before = core.store.one("SELECT reserved_requests,used_requests FROM exploration_requests")

    async def scenario():
        async with httpx.AsyncClient(
            base_url="http://core",
            transport=httpx.ASGITransport(app),
            headers={"Authorization": "Bearer " + token},
        ) as client:
            result = await client.post(
                f"/internal/v2/readers/{adapter}/{item['request_id']}/validate", json=body
            )
        assert result.status_code == status
        assert "PRIVATE_VALIDATION_CREDENTIAL" not in result.text

    run(scenario())
    assert (
        core.store.one("SELECT reserved_requests,used_requests FROM exploration_requests") == before
    )
    assert not core.store.rows("SELECT * FROM observations")


def test_successful_bilibili_read_validates_every_get_without_sending_credentials_to_core(
    core, tmp_path
):
    adapter, path, _, app = setup_read(core, tmp_path, "bilibili")
    started = datetime.fromisoformat(core.clock())
    validations = []

    def boundary(request, result):
        if request.url.path.endswith("/validate"):
            assert result.status_code == 200
            validations.append(json.loads(request.content))

    async def scenario():
        pool, backend = mock_pool(response(META), response(PLAYER), response(SUBTITLE))
        async with BilibiliAccountHTTP(path, pool, clock=lambda: started) as account:
            async with httpx.AsyncClient(
                base_url="http://core",
                transport=BoundaryTransport(app, boundary),
                headers={
                    "Authorization": "Bearer "
                    + core.settings.observation_adapters[adapter].token.get_secret_value()
                },
            ) as client:
                assert await tick(client, BilibiliReader(account, clock=core.clock), adapter)
        assert len(backend.requests) == len(validations) == 3
        assert "fixture-only-isolated-account-cookie" not in json.dumps(validations)
        assert all(set(item) == {"lease_token", "source_grant"} for item in validations)
        assert len(core.store.rows("SELECT * FROM source_grant_events")) == 1

    run(scenario())

"""Owner control routes; transport credentials cannot create or widen permission."""

from companion import owner_authority
from companion.owner_authority_protocol import AuthorityChange, AuthorityPreview, AuthorityStop


def register_owner_authority_routes(app, control_auth):
    prefix = "/internal/v1/owner-authority"

    async def run(operation, *args):
        core = app.state.core
        return await app.state.api_store.run(
            lambda store: operation(store, core.settings, core.clock, *args))

    @app.post(prefix + "/preview", dependencies=control_auth)
    async def preview(body: AuthorityPreview):
        core = app.state.core

        def execute(store):
            return store.read_or_write(
                lambda: owner_authority.preview(store, core.settings, core.clock, body))

        return await app.state.api_store.run(execute)

    @app.post(prefix + "/activate", dependencies=control_auth)
    @app.post(prefix + "/revisions", dependencies=control_auth)
    async def activate(body: AuthorityChange):
        return await run(owner_authority.activate, body)

    @app.post(prefix + "/stop", dependencies=control_auth)
    async def stop(body: AuthorityStop):
        return await run(owner_authority.stop, body)

    @app.get(prefix + "/status", dependencies=control_auth)
    async def status():
        return await run(owner_authority.status)

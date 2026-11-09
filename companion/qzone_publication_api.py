"""The same native review operations through owner control or same-origin Console auth."""

from fastapi import Header, Request, Response

from companion.qzone_publication import (
    PREFIX,
    PublicationCreate,
    PublicationDecision,
    PublicationEdit,
    decide,
    edit,
    prepare,
    view,
)
from companion.social_autonomy import SocialAutonomy


def register_routes(app, settings, prefix, auth, request_check):
    def module(store=None):
        return SocialAutonomy(store or app.state.core.store, settings, clock=app.state.core.clock)

    async def mutate(request, body, key, action, *args):
        check = request_check(request)
        return await app.state.api_store.run(lambda store: action(
            module(store), *args, body, key, owner_check=lambda: check(store)))

    @app.get(prefix, dependencies=auth)
    async def publications(response: Response):
        response.headers["Cache-Control"] = "no-store"
        service = module()
        rows = service.store.rows('SELECT plan_id FROM social_autonomy_plans WHERE idempotency_key '
            'LIKE %s ORDER BY record_sequence DESC NULLS LAST LIMIT 30', (PREFIX + "%",))
        return {"items": [view(service, row["plan_id"]) for row in rows]}

    @app.get(prefix + "/{plan_id}", dependencies=auth)
    async def publication(plan_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return view(module(), plan_id)

    @app.post(prefix, dependencies=auth, status_code=201)
    async def create(body: PublicationCreate, request: Request,
                     idempotency_key: str = Header(default="")):
        return await mutate(request, body, idempotency_key, prepare)

    @app.put(prefix + "/{plan_id}", dependencies=auth)
    async def update(plan_id: str, body: PublicationEdit, request: Request,
                     idempotency_key: str = Header(default="")):
        return await mutate(request, body, idempotency_key, edit, plan_id)

    @app.post(prefix + "/{plan_id}/decision", dependencies=auth)
    async def decision(plan_id: str, body: PublicationDecision, request: Request,
                       idempotency_key: str = Header(default="")):
        return await mutate(request, body, idempotency_key, decide, plan_id)

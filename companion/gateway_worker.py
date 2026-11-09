"""Local management and independent Gateway execution process."""

import argparse
import asyncio
import json
from contextlib import closing
from pathlib import Path

import httpx
import uvicorn

from companion import gateway
from companion.config import Settings
from companion.gateway_api import create_app
from companion.gateway_contract import ClientGrant
from companion.model import ModelClient
from companion.postgres import Database
from companion.store import Store, migrate, utcnow


async def worker(store, settings, *, once=False):
    async with httpx.AsyncClient(timeout=settings.model_timeout("gateway"),
                                 trust_env=False, follow_redirects=False) as client:
        model = ModelClient(settings, client)
        while True:
            progressed = await gateway.run_once(store, settings, model)
            if once:
                return
            if not progressed:
                await asyncio.sleep(0.25)


def main():
    parser = argparse.ArgumentParser(description="Private Gateway G1a (non-streaming text)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "serve", "expire"):
        commands.add_parser(name)
    run = commands.add_parser("worker")
    run.add_argument("--once", action="store_true")
    put = commands.add_parser("client-put")
    put.add_argument("--grant", required=True)
    revoke = commands.add_parser("client-revoke")
    revoke.add_argument("--client-id", required=True)
    purge = commands.add_parser("client-purge")
    purge.add_argument("--client-id", required=True)
    args = parser.parse_args()
    settings = Settings.load()
    if args.command == "serve":
        current = gateway.config()
        uvicorn.run(create_app(settings), host=current.bind_host, port=current.port,
                    proxy_headers=False, access_log=False)
        return
    if args.command == "init":
        migrate(settings.database)
    with Database(settings.database, max_connections=1) as database, closing(
        Store(database, settings.state_directory),
    ) as store:
        if args.command == "init":
            print(json.dumps(gateway.budget_reference(store, settings, utcnow())))
        elif args.command == "client-put":
            grant = ClientGrant.model_validate_json(Path(args.grant).read_bytes())
            token = gateway.put_client(store, settings, grant, utcnow())
            print(json.dumps({"client_id": grant.client_id, "api_key": token}))
        elif args.command == "client-revoke":
            gateway.revoke_client(store, args.client_id, utcnow())
        elif args.command == "client-purge":
            gateway.revoke_client(store, args.client_id, utcnow())
            with store.transaction():
                store.execute('UPDATE gateway_requests SET request_cipher = NULL, response_cipher '
                    "= NULL, state = 'EXPIRED' WHERE client_id = %s", (args.client_id,))
        elif args.command == "worker":
            asyncio.run(worker(store, settings, once=args.once))
        elif args.command == "expire":
            gateway.expire(store, settings, utcnow())


if __name__ == "__main__":
    main()

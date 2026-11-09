"""Content-free ASGI timing; route labels come only from FastAPI's registry.

Observe send completion rather than buffering a stream or replacing a response.
Handled HTTP errors have no observable Python exception class. An exception before
response.start has status=None: an outer server error response is not observed here.
"""

import asyncio
from time import perf_counter_ns

import psycopg

from companion import telemetry


class ApiTelemetry:
    def __init__(self, app, *, routes):
        self.app = app
        self.templates = {id(route): route.path_format for route in routes
                          if isinstance(getattr(route, "path_format", None), str)
                          and len(route.path_format) <= 512}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not telemetry.api_enabled():
            return await self.app(scope, receive, send)
        started = perf_counter_ns()
        status, error_class, complete, finished, trailers = None, None, False, None, False

        async def observed_send(message):
            nonlocal status, complete, finished, trailers
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
                trailers = message.get("trailers", False)
            elif message["type"] in {"http.response.body", "http.response.zerocopysend"}:
                complete = not message.get("more_body", False) and not trailers
            elif message["type"] == "http.response.pathsend" or (
                message["type"] == "http.response.trailers"
                and not message.get("more_trailers", False)
            ):
                complete = True
            if complete and finished is None:
                finished = perf_counter_ns()

        try:
            await self.app(scope, receive, observed_send)
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                error_class = "CancelledError"
            elif isinstance(error, psycopg.OperationalError):
                error_class = "OperationalError"
            elif isinstance(error, psycopg.DatabaseError):
                error_class = "DatabaseError"
            else:
                error_class = "UnhandledError"
            raise
        finally:
            if error_class is None and status is not None and status >= 400:
                error_class = "HTTPError"
            ended = finished if finished is not None else perf_counter_ns()
            telemetry.emit_api(
                self.templates.get(id(scope.get("route")), "UNMATCHED"),
                scope.get("method") if scope.get("method") in telemetry.API_METHODS else "OTHER",
                status=status,
                elapsed_ms=(ended - started) / 1e6,
                error_class=error_class, response_complete=complete,
            )

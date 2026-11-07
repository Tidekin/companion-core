import httpx

from tests.support.onebot_images import URL


class ConnectThenGet(httpx.AsyncBaseTransport):
    def __init__(self, delegate, *, failure="tcp", addresses=None):
        self.delegate, self.failure = delegate, failure
        self.addresses = addresses if addresses is not None else []
        self.gets = 0

    async def handle_async_request(self, request):
        self.addresses.append(request.url.host)
        trace = request.extensions["trace"]
        if len(self.addresses) == 1:
            if self.failure in {"tcp", "tls"}:
                name = "connect_tcp" if self.failure == "tcp" else "start_tls"
                await trace("connection." + name + ".failed", {})
            elif self.failure == "dispatched":
                await trace("http11.send_request_headers.started", {})
                self.gets += 1
            raise httpx.ConnectError(URL, request=request)
        await trace("http11.send_request_headers.started", {})
        self.gets += 1
        return await self.delegate.handle_async_request(request)




class NoNetworkClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

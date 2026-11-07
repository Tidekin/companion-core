import json
import sys
from pathlib import Path

import httpx

if __name__ == "__main__" and sys.argv[1:] == ["--reader"]:
    import runpy
    from functools import partialmethod

    import source_reader.bilibili as bili
    import source_reader.heybox as heybox
    import source_reader.network as network
    from tests.support.heybox_reader import mock_pool
    from tests.support.source_reader import META, response

    packet = json.loads(sys.stdin.buffer.read())
    raw = response(META) if packet["source"] == "bilibili" else response(
        b"<title>post</title><article><p>synthetic article evidence</p></article>")
    if packet["discover"]:
        raw = response({"code": 0, "data": {"list": [{
            "bvid": packet["url"].rsplit("/", 1)[1], "title": "public title"}]}}) if (
            packet["source"] == "bilibili") else response(
                ('<a href="' + packet["url"] + '">public title</a>').encode())
    pool, backend = mock_pool(raw)
    network.PublicHTTP.__init__ = partialmethod(network.PublicHTTP.__init__, pool=pool)
    for reader in (bili.BilibiliReader, heybox.HeyboxReader):
        reader.__init__ = partialmethod(reader.__init__, clock=lambda: packet["clock"])
    original_post = httpx.AsyncClient.post

    async def post(client, path, **kwargs):
        if packet["lose_report"] and path.endswith("/result"):
            raise httpx.ReadTimeout("synthetic result report loss")
        return await original_post(client, path, **kwargs)

    httpx.AsyncClient.post = post
    sys.argv = ["source_reader", *packet["args"]]
    runpy.run_module("source_reader", run_name="__main__")
    assert all(row.startswith(b"GET ") and b"authorization:" not in row.lower()
               for row in backend.requests)
    Path(packet["report"]).write_text(json.dumps({"requests": len(backend.requests)}))

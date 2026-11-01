"""Actual Reader CLI with only platform HTTP replaced by a deterministic transport."""

import importlib.abc
import json
import os
import runpy
import sys
from datetime import datetime
from urllib.parse import parse_qs, urlsplit


class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"sqlite3", "fastapi", "pydantic", "alembic"}:
            raise ImportError("FORBIDDEN_READER_DEPENDENCY:" + fullname)
        if fullname.startswith("companion.") and fullname != "companion.source_policy":
            raise ImportError("FORBIDDEN_CORE_MODULE:" + fullname)


def main():
    payload = json.loads(sys.stdin.read())
    sys.meta_path.insert(0, Boundary())
    import httpcore

    from source_reader import qzone

    now = datetime.fromisoformat(payload["now"])
    raw = json.dumps(payload["response"], ensure_ascii=False).encode()
    wire = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(raw)).encode() + b"\r\nConnection: close\r\n\r\n" + raw)
    calls = []

    class ProviderPool(httpcore.AsyncConnectionPool):
        def stream(self, method, url, **kwargs):
            parsed = urlsplit(url)
            assert method == "GET" and url.startswith(qzone.ENDPOINT + "?")
            query = parse_qs(parsed.query)
            assert query["uin"] == query["hostuin"] == [payload["target_uin"]]
            assert query["count"] == ["1"] and query["start"] == ["0"]
            assert kwargs["headers"]["Referer"] == (
                "https://user.qzone.qq.com/" + payload["target_uin"])
            calls.append({"method": method, "path": parsed.path})
            return super().stream(method, url, **kwargs)

    original_http, original_reader = qzone.QZoneHTTP, qzone.QZoneReader
    qzone.QZoneHTTP = lambda path: original_http(path, ProviderPool(
        network_backend=httpcore.AsyncMockBackend([wire])), clock=lambda: now)
    qzone.QZoneReader = lambda public: original_reader(public, clock=lambda: now)
    sys.argv = ["source_reader", "--source", "qzone", "--protocol", "reading-v1",
                "--account-file", payload["account_file"], "--adapter-id", "qzone",
                "--core-url", payload["origin"], "--once"]
    runpy.run_module("source_reader", run_name="__main__")
    print(json.dumps({"pid": os.getpid(), "calls": calls}))


if __name__ == "__main__":
    main()

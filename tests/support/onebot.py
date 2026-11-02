"""Synthetic OneBot responses and authenticated loopback HTTP, without Core imports."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

# Owner-conversation history served by `get_friend_msg_history`; tests fill it.
HISTORY = []


def provider(request, sends, receipt=None):
    action = request.url.path
    if action == "/get_login_info":
        data = {"user_id": 10002}
    elif action == "/get_status":
        data = {"online": True, "good": True}
    elif action == "/get_version_info":
        data = {"app_name": "SnowLuma", "app_version": "2.0.3-node"}
    elif action == "/get_friend_msg_history":
        assert json.loads(request.content) == {"user_id": 10001, "count": 20}
        data = {"messages": list(HISTORY)}
    elif action in {"/get_msg", "/get_forward_msg"}:
        return httpx.Response(200, json={"status": "failed", "retcode": 100, "data": None})
    elif action == "/send_private_msg":
        body = json.loads(request.content)
        assert body["user_id"] == 10001
        assert set(body) == {"user_id", "message", "auto_escape"}
        segments = body["message"]
        if segments[0]["type"] == "reply":  # One native quote may precede the text.
            assert set(segments[0]["data"]) == {"id"} and isinstance(segments[0]["data"]["id"], str)
            segments = segments[1:]
        if len(segments) == 1 and segments[0]["type"] == "image":
            assert set(segments[0]["data"]) == {"file"}
            assert segments[0]["data"]["file"].startswith("base64://")
        else:
            assert segments[0]["type"] == "text"
        sends.append(body)
        if isinstance(receipt, Exception):
            raise receipt
        if receipt is not None:
            return httpx.Response(200, json=receipt)
        data = {"message_id": 123456}
    else:
        import pytest

        pytest.fail("unexpected OneBot action")
    return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})


@contextmanager
def onebot_http(sends, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer " + token
            body = self.rfile.read(int(self.headers["Content-Length"]))
            response = provider(
                httpx.Request("POST", "http://mock" + self.path, content=body), sends
            )
            self.send_response(response.status_code)
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

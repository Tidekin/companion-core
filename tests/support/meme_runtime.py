import hashlib
import json
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx

from companion_onebot_bridge.meme_runtime import BASE
from tests.support.native_meme import case
from tests.support.next_memes_catalog import png
from tests.support.next_memes_packs import PACK, REVISION, archive

LICENSE_BYTES = b"Synthetic fixture license: CC0-1.0. Test-authored pixels and captions.\n"


RULE = {
    "repo": "example/fixture",
    "revision": REVISION,
    "license": "CC0-1.0",
    "license_path": "LICENSE",
    "license_sha256": hashlib.sha256(LICENSE_BYTES).hexdigest(),
    "asset_prefix": "memes",
    "usage_basis": "Test fixture approved for private message reuse",
}


def item(event, candidate, *, key=None):
    return {
        "message_id": "msg_" + "a" * 32,
        "window_origin_event_id": event["event_id"],
        "delivery_key": key or "reply:" + event["event_id"] + ":v1",
        "reply_revision": 1,
        "content": {
            "text": "太棒了。",
            "image": {
                "source": "companion.meme_catalog",
                "offer_key": "meme:" + event["event_id"],
                "candidate_id": candidate,
                "category": "happy",
            },
        },
    }


class CoreHTTP:
    """Real API with faults applied only after actual authorization and response."""

    def __init__(self, runtime, event, *, provider="community", lose_results=0,
                 unknown=False, reject_check=False):
        self.runtime, self.event = runtime, event
        self.core = case(runtime).core
        self.calls, self.results = [], []
        self.provider = provider
        self.lose_results, self.unknown, self.reject_check = lose_results, unknown, reject_check
        self.claim = None
        self.token = runtime.config.bridge_token
        self.deadline = self.core.clock.now + timedelta(seconds=self.core.settings.lease_seconds)

    async def provision(self, client, event=None, *, revision=1):
        event = event or self.event
        while await self.runtime.window.flush_one(client):
            pass
        # A completed foreground turn makes background discovery eligible. Native
        # foreground-worker execution is covered separately; this is a precondition.
        self.core.store.execute((
            "UPDATE jobs SET state='SUCCEEDED' WHERE kind='chat' AND "
            'event_id=%s'
        ), (event["event_id"],))
        response = await client.post(BASE + "/submit", json={
            "event_id": event["event_id"], "reply_revision": revision,
            "intent": "celebrate",
            "provider": self.provider})
        assert response.status_code == 200, response.text
        return response.json()

    def handle(self, response):
        request = response.request
        path, body = request.url.path, json.loads(request.content)
        self.calls.append((path, body))
        if path == BASE + "/lease" and response.status_code == 200:
            self.claim = response.json()["item"] or self.claim
        if path.endswith("/check") and self.reject_check:
            response.status_code = 503
            response._content = b'{"error":"SYNTHETIC_CORE_GATE_REFUSED"}'
        if path.endswith("/result"):
            self.results.append(body)
            if self.lose_results:
                self.lose_results -= 1
                raise httpx.ReadTimeout("synthetic lost result ACK", request=request)
        return response

    async def response(self, response):
        if response.request.url.path.startswith(BASE + "/"):
            await response.aread()
            self.handle(response)

    async def request(self, request):
        if self.unknown and request.url.path.endswith("/result"):
            # Inject a retained earlier uncertain attempt; the real settlement
            # endpoint must keep its UNKNOWN liability and observed usage.
            self.core.store.execute("UPDATE meme_discovery_reservations SET state='UNKNOWN'")

    @asynccontextmanager
    async def client(self):
        async with case(self.runtime).client(self.runtime) as client:
            if (self.runtime.library.enabled and not self.runtime.db.execute(
                "SELECT 1 FROM meme_runtime_results LIMIT 1").fetchone()):
                try:
                    self.runtime._event_check(self.event["event_id"])
                except ValueError:
                    pass
                else:
                    await self.provision(client)
            client.event_hooks["request"].append(self.request)
            client.event_hooks["response"].append(self.response)
            yield client


def public_transport(calls, *, semantic=True, license_bytes=LICENSE_BYTES, brave=False):
    def handle(request):
        calls.append(request)
        assert request.url.host == "93.184.216.34"
        host = request.headers["Host"]
        assert request.extensions["sni_hostname"] == host
        if brave and host == "api.search.brave.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://example.org/source",
                            "title": "celebrate",
                            "properties": {"url": "https://example.org/image.png"},
                            "verified": True,
                        }
                    ]
                },
            )
        if brave:
            return httpx.Response(200, content=png(), headers={"content-type": "image/png"})
        if host == "raw.githubusercontent.com":
            if request.url.path.endswith("/community-index.json"):
                return httpx.Response(200, json={"schema_version": "1.0.0", "packs": [PACK]})
            root = f"/example/fixture/{REVISION}/"
            if request.url.path == root + "manifest.json":
                return httpx.Response(200, json={
                    "schema_version": 1, "id": "fixture-pack", "version": "1.0",
                    "license": RULE["license"],
                    "categories": {"happy": {"description": "庆祝 celebrate 成功"}},
                })
            if request.url.path == root + "LICENSE":
                return httpx.Response(200, content=license_bytes)
            assert request.url.path == root + "memes/happy/a.png"
            return httpx.Response(200, content=png(), headers={"content-type": "image/png"})
        if host == "api.github.com":
            if "/contents/" in request.url.path:
                assert request.url.path == "/repos/example/fixture/contents/memes/happy"
                assert request.url.query == ("ref=" + REVISION).encode()
                data = png()
                blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
                return httpx.Response(200, json=[{
                    "name": "a.png", "path": "memes/happy/a.png", "type": "file",
                    "size": len(data),
                    "sha": blob,
                }])
            return httpx.Response(200, json={"sha": REVISION})
        assert host == "codeload.github.com"
        return httpx.Response(
            200,
            content=archive(
                semantic=semantic,
                extra=("fixture-" + REVISION + "/LICENSE", license_bytes),
            ),
        )

    return httpx.MockTransport(handle)

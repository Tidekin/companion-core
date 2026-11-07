import asyncio
import os

import pytest

from source_reader.browser import BrowserRenderer
from source_reader.network import ReadFailure
from tests.conftest import run
from tests.support.exploration import URL

pytestmark = pytest.mark.skipif(
    os.environ.get("COMPANION_TEST_BROWSER") != "1", reason="Explicit sandboxed Chromium test run"
)


def test_browser_executes_inline_metadata_offline_without_subresources(tmp_path):
    async def scenario():
        renderer = BrowserRenderer(timeout=8)
        document = b"""<html><script>
          window.__INITIAL_STATE__ = {videoData:{title:'rendered',desc:'real inline description'}};
          fetch('http://127.0.0.1:1/private').then(()=>{window.__INITIAL_STATE__.videoData.desc='leak'});
          new WebSocket('ws://127.0.0.1:1/');
          window.open('https://evil.example/');
          </script><iframe src="file:///etc/passwd"></iframe>
          <script src="https://api.bilibili.com/x/member/info"></script></html>"""
        result = await renderer.metadata(URL, document)
        assert result["desc"] == "real inline description"
        assert renderer.active_contexts == 0
        result = await renderer.metadata(URL, b'<meta property="og:description" content="next">')
        assert result["desc"] == "next" and result["title"] == ""

    run(scenario())


def test_browser_timeout_closes_session_and_next_task_still_works():
    async def scenario():
        renderer = BrowserRenderer(timeout=1)
        with pytest.raises((ReadFailure, TimeoutError)):
            await renderer.metadata(URL, b"<script>while(true){}</script>")
        assert renderer.active_contexts == 0
        renderer.timeout = 8
        result = await renderer.metadata(URL, b'<meta property="og:title" content="healthy">')
        assert result["title"] == "healthy"

    run(scenario())


def test_browser_cancel_closes_session():
    async def scenario():
        renderer = BrowserRenderer(timeout=15)
        task = asyncio.create_task(renderer.metadata(URL, b"<script>while(true){}</script>"))
        for _ in range(100):
            if renderer.active_contexts:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert renderer.active_contexts == 0

    run(scenario())


def test_browser_cannot_send_webrtc_packets_to_a_private_address():
    async def scenario():
        packets = []

        class Receiver(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                packets.append(data)

        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            Receiver, local_addr=("127.0.0.1", 0)
        )
        port = transport.get_extra_info("sockname")[1]
        document = f"""<script>
          window.__INITIAL_STATE__ = {{videoData:{{title:'test',desc:'UDP isolation'}}}};
          const pc = new RTCPeerConnection({{iceServers:[{{urls:'stun:127.0.0.1:{port}'}}]}});
          pc.createDataChannel('probe');
          pc.createOffer().then(o => pc.setLocalDescription(o));
          Object.defineProperty(window.__INITIAL_STATE__.videoData, 'title', {{get() {{
            const end = Date.now() + 1500;
            while(Date.now() < end) {{}}
            return 'test';
          }}}});
        </script>""".encode()
        try:
            result = await BrowserRenderer(timeout=8).metadata(URL, document)
            assert result["desc"] == "UDP isolation"
            await asyncio.sleep(0.1)
            assert not packets
        finally:
            transport.close()

    run(scenario())


def test_runtime_policy_blocks_stun_even_without_javascript_guard(monkeypatch):
    from source_reader import browser

    # Isolate the native network-policy layer; the separate child-realm test
    # requires the real JS guard before untrusted scripts and rejects replacements.
    monkeypatch.setattr(browser, "_DISABLE_RTC", "")

    async def scenario():
        packets = []

        class Receiver(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                packets.append(data)

        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            Receiver, local_addr=("127.0.0.1", 0),
        )
        port = transport.get_extra_info("sockname")[1]
        document = f"""<script>
          const pc = new RTCPeerConnection({{iceServers:[{{urls:'stun:127.0.0.1:{port}'}}]}});
          pc.createDataChannel('probe');
          window.probe = pc.createOffer().then(async offer => {{
            await pc.setLocalDescription(offer);
            await new Promise(resolve => setTimeout(resolve, 1500));
            return {{native:typeof RTCPeerConnection === 'function',
                     localDescription:pc.localDescription.type}};
          }});
        </script>""".encode()
        try:
            result = await BrowserRenderer(timeout=8)._render(URL, document, "() => window.probe")
            assert result == {"native": True, "localDescription": "offer"}
            assert not packets
        finally:
            transport.close()

    run(scenario())

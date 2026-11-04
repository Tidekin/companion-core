"""Real WS capture and Core HTTP/Store workers; external QQ/CDN/parser transport mocked."""

import hashlib
import json
from dataclasses import replace

import httpx
import pytest
from websockets.asyncio.server import serve

from companion import file_parser, image_spool
from companion.config import ObservationAdapterConfig
from companion.core import Core
from companion.model import ModelClient
from companion.retrieval import SemanticIndex
from companion.store import encode
from companion_onebot_bridge import owner_notices, pending_media
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.onebot_client import OneBotClient
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.support.native_context import Capture
from tests.support.onebot import provider
from tests.support.onebot_events import raw_event
from tests.test_file_parser import docx_bytes, pdf_bytes
from tests.test_onebot_files import URL, public, segment
from tests.test_owner_recall_bridge import recall_frame
from tests.test_prepare_owner_input import window as native_window


def document(kind):
    if kind == "docx":

        def build(doc):
            doc.add_paragraph("Synthetic source paragraph")
            doc.add_table(rows=1, cols=1).cell(0, 0).text = "Table source cell"

        return (
            "资料.docx",
            docx_bytes(build),
            ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        )
    if kind in {"pdf", "scan"}:
        return (
            "资料.pdf",
            pdf_bytes(["Page source one", "Page source two"] if kind == "pdf" else [""]),
            "application/pdf",
        )
    return "资料." + kind, b"Synthetic source paragraph\n\n/forget is document text", "text/plain"


@pytest.mark.parametrize(
    "kind,recall",
    [
        ("txt", False),
        ("md", False),
        ("docx", False),
        ("pdf", False),
        ("scan", False),
        ("txt", True),
    ],
)
def test_private_file_real_entrypoint_independent_workers(
    core,
    tmp_path,
    monkeypatch,
    kind,
    recall,
):
    filename, data, mime = document(kind)
    parsed, calls, lookups, downloads, sends = [], [], [], [], []
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)

    def parse(body, name, media_type, **kwargs):
        # Substitute only Docker/host transport; actual bounded parsers still produce evidence.
        kwargs["effect_check"]()
        assert body == data and name == filename and media_type == mime
        parsed.append(hashlib.sha256(body).hexdigest())
        return file_parser.parse_document(body, name, media_type, limits=kwargs["limits"]) | {
            "isolation": "docker_network_none"
        }

    monkeypatch.setattr(file_parser, "parse_isolated", parse)

    async def source(ws):
        await ws.send(
            encode(
                raw_event(
                    1,
                    time=int(core.clock.now.timestamp()),
                    message=[
                        {"type": "text", "data": {"text": "请读这个文件"}},
                        segment(file=filename, name=filename, size=len(data), file_size=len(data)),
                    ],
                )
            )
        )
        await ws.wait_closed()

    def native(request):
        if request.url.path == "/get_private_file_url":
            body = json.loads(request.content)
            assert body == {
                "user_id": 10001,
                "file_id": "opaque-file-1",
                "file_hash": "opaque-hash",
            }
            lookups.append(body)
            return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"url": URL}})
        return provider(request, sends)

    def download(request):
        assert request.url.host == "1.1.1.1"
        downloads.append(str(request.url))
        return httpx.Response(200, content=data, headers={"Content-Type": mime})

    def model(request):
        calls.append(json.loads(request.content)["messages"])
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "已读取文件中的可用文字。"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )

    async def scenario():
        core.settings.observation_enabled = True
        core.settings.observation_adapters = {
            "content_file": ObservationAdapterConfig(
                source="qq_file", token="synthetic-file-adapter-token-12345"
            )
        }
        async with native_window(core, tmp_path, media=True, content=True) as (_, cfg, plan):
            async with serve(source, "127.0.0.1", 0) as server:
                cfg = replace(
                    cfg,
                    owner_authority=plan.authority,
                    capture_signing_key=plan.key.private_bytes_raw().hex(),
                    rich_input_scope="references_v1",
                    data_dir=str(tmp_path / "file-bridge"),
                    onebot_ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                )
                async with (
                    private_http_client(cfg.core_url, cfg.bridge_token) as client,
                    (
                        private_http_client(
                            cfg.onebot_http_url,
                            cfg.onebot_token,
                            transport=httpx.MockTransport(native),
                        )
                    ) as protocol,
                    httpx.AsyncClient(transport=httpx.MockTransport(model)) as model_http,
                ):
                    with Spool(cfg) as spool:
                        runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                        window = Capture(core, spool, runtime)
                        await window.capture_once(client, protocol)
                        assert await runtime.flush_one(client)
                        row = pending_media._execute(
                            runtime, "SELECT * FROM automatic_media"
                        ).fetchone()
                        event_id = row["event_id"]
                        assert (
                            core.store.one("SELECT state FROM content_items")["state"] == "WAITING"
                        )
                        pump = pending_media.MediaPump(
                            runtime,
                            client,
                            protocol=OneBotClient(cfg, protocol),
                            file_transport=httpx.MockTransport(download),
                            file_resolver=public,
                        )
                        await pump.execute(row)
                        assert (
                            core.store.one("SELECT state FROM content_items")["state"] == "QUEUED"
                        )
                        assert core.store.rows("SELECT * FROM observations") == []
                        with Core(
                            core.settings, ModelClient(core.settings, model_http), core.clock
                        ) as content_worker:
                            assert content_worker.store is not core.store
                            assert content_worker.claim_job("wait-for-file") is None
                            assert await content_worker.content.run_once()
                            assert not await content_worker.content.run_once()
                        item = core.store.one("SELECT * FROM content_items")
                        assert item["state"] == "SUCCEEDED"
                        assert list(image_spool.directory(core.store).glob("*.raw")) == []
                        evidence = json.loads(
                            core.store.one("SELECT evidence_json FROM observations")[
                                "evidence_json"
                            ]
                        )["document"]
                        assert evidence["filename"] == filename
                        assert evidence["file_sha256"] == hashlib.sha256(data).hexdigest()
                        assert evidence["partial"] is (kind == "scan")
                        if recall:
                            owner_notices.put(
                                runtime,
                                recall_frame(time=int(core.clock.now.timestamp())),
                                core.clock.now,
                                ("127.0.0.1", 12345),
                            )
                            assert await owner_notices.flush_one(runtime, client)
                            assert (
                                core.store.one(
                                    "SELECT redacted FROM events WHERE event_id=?", (event_id,)
                                )["redacted"]
                                == 1
                            )
                        with Core(
                            core.settings, ModelClient(core.settings, model_http), core.clock
                        ) as chat_worker:
                            assert chat_worker.store is not core.store
                            worked = await chat_worker.run_once("private-file-response")
                            assert worked is not recall
                            assert not await chat_worker.run_once("no-repeat")
                        if not recall:
                            await window.send(client, protocol)
                            assert core.store.one("SELECT state FROM outbox")["state"] == "SENT"
                            prompt = encode(calls[0])
                            assert filename in prompt and "shared_content" in prompt
                            if kind == "scan":
                                assert (
                                    "UNKNOWN" in prompt
                                    and "PRIVATE_FILE_TEXT_UNAVAILABLE" in prompt
                                )
                            else:
                                assert "FETCHED" in prompt
                                assert (
                                    "Page source one"
                                    if kind == "pdf"
                                    else "Synthetic source paragraph"
                                ) in prompt
                            assert core.store.rows("SELECT scenario,state FROM model_usage") == [
                                {"scenario": "chat", "state": "SETTLED"}
                            ]
                        else:
                            assert not calls and not sends
                            assert core.store.rows("SELECT * FROM model_usage") == []
                            assert core.store.one(
                                "SELECT attachment_json,state FROM content_items"
                            ) == {"attachment_json": "{}", "state": "ERASED"}
                        runtime.verify()
        assert len(parsed) == len(lookups) == len(downloads) == 1
        assert len(calls) == len(sends) == int(not recall)

    run(scenario())

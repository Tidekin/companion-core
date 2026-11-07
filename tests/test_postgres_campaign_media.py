"""The actual campaign importer accepts synthetic image/file/ASR/reference inputs."""

import hashlib
import importlib.util
import io
import json
import sys
import wave
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PIL import Image

from companion import owner_authority, paid_budget
from companion.core import Core
from companion.file_parser import parse_document
from companion.model import ModelClient
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from tests.conftest import run
from tests.sdk_reader_fixture import answer
from tests.support.owner_authority import activate
from tests.test_paid_model_gate import approved_live


@pytest.mark.parametrize("mode", ["image", "file", "voice", "quote"])
def test_campaign_media_import_keeps_original_source_and_fee_custody(
    core, tmp_path, monkeypatch, mode,
):
    root = Path(__file__).resolve().parents[1]
    runner = root / "benchmarks/memory_campaign/runner"
    sys.path.insert(0, str(runner))
    if "scripts" in sys.modules:
        sys.modules["scripts"].__path__.append(str(runner / "scripts"))
    from scripts.memory_benchmark import runtime

    stream = io.BytesIO()
    if mode == "image":
        Image.new("RGB", (16, 12), "blue").save(stream, format="PNG")
        name = "synthetic.png"
    elif mode == "voice":
        with wave.open(stream, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x00" * 32000)
        name = "synthetic.wav"
    else:
        stream.write("相机放在蓝色箱子里".encode())
        name = "synthetic.txt"
    blob = stream.getvalue()
    (tmp_path / name).write_bytes(blob)
    asr = tmp_path / "asr.jsonl"
    asr.write_text(json.dumps({"asset": name, "sha256": hashlib.sha256(blob).hexdigest(),
                               "text": "相机放在蓝色箱子里", "segments": [{"start_seconds": 0.0,
                                   "end_seconds": 2.0, "text": "相机放在蓝色箱子里"}],
                               "language": "zh",
                               "language_probability": 1.0, "duration_seconds": 2.0,
                               "partial": False}) + "\n", "utf8")
    context = {"source_snapshot": str(root), "suite_dir": str(tmp_path), "asr_file": str(asr),
               "run_dir": str(tmp_path), "provider": {"base_url": "https://synthetic.invalid/v1"},
               "pricing": {"input_rate": 2., "cached_input_rate": .04,
                           "output_rate": 8., "safety_factor": 1.5},
               "concurrency": 1, "budget": {"total_cny": 2., "inference_cny": 1.},
               "parser": {"image": "sha256:" + "a" * 64, "transport": "docker"}}
    monkeypatch.setattr(runtime, "context", lambda: context)
    spec = importlib.util.spec_from_file_location(
        "synthetic_campaign_worker", runner / "scripts/memory_benchmark/worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    approved_live(core, tmp_path, concurrency_basis="local_active_attempts_v2")
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    core.settings.memory_enabled = True
    monkeypatch.setattr(module, "settings", lambda directory: (core.settings, "d" * 64))
    cfg, _ = module.config(tmp_path)

    def external_parser(data, filename, mime, *, limits, effect_check, **kwargs):
        # Only the external process transport is replaced in this importer check.
        effect_check()
        return parse_document(data, filename, mime, limits=limits) | {
            "isolation": "docker_network_none"}

    monkeypatch.setattr(module, "parse_remote", external_parser)
    key = Ed25519PrivateKey.generate()
    purposes = ["chat", "memory", "history", "image", "file", "voice", "content"]
    policy = OwnerGrantPolicy(
        owner_qq_id=cfg.owner_qq_id, purposes=purposes, memory_scope="owner_private",
        authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(cfg)),
        capability_policies={k: v for k, v in owner_authority.capability_hashes(cfg).items()
                             if k in purposes},
        history={"scope": "owner_private", "not_before": core.clock(),
                 "max_turns": 14, "max_utf8_bytes": 32000},
        capture={"public_key": key.public_key().public_bytes_raw().hex(),
                 "identity_sha256": paid_budget.sha([cfg.owner_qq_id, cfg.bot_qq_id]),
                 "protocol_epoch": "public-memory-benchmark-v1"})
    authority = activate(core, policy)[0]["authority"]
    calls = []

    def model(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=answer("相机放在蓝色箱子里"))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
            with Core(cfg, ModelClient(cfg, http), core.clock) as worker:
                harness = module.Mixed(worker, key, authority, tmp_path)
                await harness.import_source({"id": "synthetic-source", "turn": 1,
                    "modality": mode, "text": "" if mode != "quote" else "资料如下",
                    "caption": "", "asset": name, "assistant_reply": "记住了",
                    "quoted": {"text": "相机放在蓝色箱子里", "uin": "10001", "name": "主人"}})
                assert harness.imported == 1
                assert not (tmp_path / "media-errors.jsonl").exists(), (
                    tmp_path / "media-errors.jsonl").read_text("utf8")
                assert worker.store.one("SELECT state FROM jobs WHERE kind='chat'") == {
                    "state": "SUCCEEDED"}
                assert calls
                assert all(row["state"] == "SETTLED" for row in worker.store.rows(
                    "SELECT state FROM model_usage"))
    run(scenario())

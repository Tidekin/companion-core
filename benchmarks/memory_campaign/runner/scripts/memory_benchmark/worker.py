# ruff: noqa: E402 -- load frozen Core before importing native APIs
"""Actual native Core inputs, originals, ASR results, file workers and memory."""

import asyncio
import hashlib
import json
import secrets
import shlex
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from scripts.memory_benchmark.budget import Budget, MeteredTransport
from scripts.memory_benchmark.runtime import context

CTX = context()
sys.path.insert(0, str(CTX["source_snapshot"]))
BUDGET = Budget.from_context(CTX)
STOP = asyncio.Event()

from companion import agent_images, content_files, file_parser, voice_authority
from companion.config import ObservationAdapterConfig
from companion.file_evidence import PrivateFilesConfig
from companion.image_input import ImageInputPolicy
from companion.input_debounce import InputDebounceConfig
from companion.media_config import ContentConfig
from companion.qq_rich import input_digest
from companion.voice_config import VoiceConfig, VoiceNode
from companion.voice_jobs import sign_asr_proof
from companion.voice_protocol import (
    VoiceClaimRequest,
    VoiceControlRequest,
    VoiceLeaseRef,
    VoiceTranscriptResult,
    make_voice_result,
)
from companion_delivery_protocol.authority import (
    file_upload_signing_payload,
    image_upload_signing_payload,
)
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.reference_reader import enrich
from scripts.memory_benchmark.native import (
    AuthorityChange,
    AuthorityPreview,
    Clock,
    Core,
    Ed25519PrivateKey,
    Harness,
    ModelClient,
    OwnerGrantPolicy,
    append,
    digest,
    encode,
    execution_audit,
    migrate,
    owner_authority,
    paid_budget,
    run_with_retries,
    save,
    settings,
    unknown_locally_closed,
)

SUITE = Path(CTX["suite_dir"])
PARSER = CTX["parser"]["image"]
ASR = {
    r["asset"]: r
    for r in (json.loads(s) for s in Path(CTX["asr_file"]).read_text("utf-8").splitlines())
}


def parse_remote(data, filename, mime, *, image, limits, effect_check, **_kwargs):
    """Reuse the exact native Docker argv and result validation over authorized SSH."""
    effect_check()
    name = "companion-file-fixtures-" + uuid.uuid4().hex
    argv = file_parser._container_argv("docker", image, name, filename, mime, limits)
    if CTX["parser"]["transport"] == "ssh":
        parser = CTX["parser"]
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "UserKnownHostsFile=" + parser["known_hosts_file"],
            "-p",
            str(parser["port"]),
            parser["host"],
            shlex.join(argv),
        ]
    else:
        command = argv
    completed = subprocess.run(
        command, input=data, capture_output=True, timeout=limits.wall_seconds + 20
    )
    if completed.returncode:
        raise RuntimeError("REMOTE_PARSER_FAILED:" + str(completed.returncode))
    result = json.loads(completed.stdout)
    effect_check()
    return result | {"isolation": "docker_network_none"}


def config(directory):
    cfg, auth = settings(directory)
    cfg.paid_model = cfg.paid_model.model_copy(
        update={
            "window_id": "custom-memory-" + directory.name,
            "window_micro_usd": cfg.paid_model.window_micro_usd,
            "daily_micro_usd": cfg.paid_model.daily_micro_usd,
            "monthly_micro_usd": cfg.paid_model.monthly_micro_usd,
            "window_calls": 700,
            "daily_calls": 700,
            "image_input": ImageInputPolicy(
                approval_sha256=auth,
                estimated_tokens_per_image=2000,
                max_bytes_per_image=1000000,
                max_pixels_per_image=1000000,
                max_images_per_request=2,
            ),
        }
    )
    # Logical time spans six months; retain native FX expiry with an explicit
    # conservative estimate valid for the whole synthetic period.
    cfg.cloud_budget = cfg.cloud_budget.model_copy(
        update={
            "fx_validity_seconds": 366 * 86400,
            "usd_to_cny_micro": 10000000,
            "fx_source": "synthetic_period_conservative_hold_10CNY_per_USD",
        }
    )
    cfg.input_debounce = InputDebounceConfig(enabled=False)
    cfg.rich_input_scope = "references_v1"
    cfg.content_enabled = cfg.observation_enabled = True
    cfg.content = ContentConfig(use_chat_model_for_vision=True)
    cfg.private_files = PrivateFilesConfig(enabled=True, parser_image=PARSER)
    cfg.observation_adapters = {
        "content_image": ObservationAdapterConfig(
            source="qq_image", token=secrets.token_urlsafe(32), media_analysis=True
        ),
        "content_file": ObservationAdapterConfig(source="qq_file", token=secrets.token_urlsafe(32)),
    }
    cfg.voice = VoiceConfig(
        enabled=True,
        signing_private_key=secrets.token_hex(32),
        nodes={
            "local": VoiceNode(
                token=secrets.token_urlsafe(32),
                capabilities=("asr",),
                manifest_sha256=digest(
                    {"provider": "FasterWhisper-small-CPU-int8-beam3", "scope": "synthetic"}
                ),
            )
        },
    )
    return cfg, auth


class Mixed(Harness):
    advance_exam_seconds = 0

    def __init__(self, core, key, authority, directory):
        super().__init__(core, key, authority, directory)
        self.bridge = BridgeConfig(
            owner_qq_id="10001",
            bot_qq_id="10002",
            bridge_token=core.settings.bridge_token.get_secret_value(),
            onebot_token=secrets.token_urlsafe(32),
            protocol_epoch="public-memory-benchmark-v1",
            rich_input_scope="references_v1",
            data_dir=str(directory / "unused-bridge"),
        )

    async def import_source(self, item):
        mode = item["modality"]
        turn = item["turn"]
        if mode in {"text", "assistant"}:
            self.import_turn(
                {"轮次": turn, "用户消息": item["text"], "助手回复": item["assistant_reply"]}
            )
            append(self.directory / "turns-observed.jsonl", {**item, "observed_text": item["text"]})
            return
        self.phase, self.case = "media_import", item["id"]
        caption = item["caption"] or ""
        source = None
        if mode == "image":
            source = {
                "type": "image",
                "data": {"url": "https://gchat.qpic.cn/benchmark/" + item["id"]},
            }
        elif mode == "file":
            data = (SUITE / item["asset"]).read_bytes()
            source = {
                "type": "file",
                "data": {
                    "file_id": item["id"],
                    "file": Path(item["asset"]).name,
                    "file_size": len(data),
                },
            }
        elif mode in {"voice", "relative_voice"}:
            source = {
                "type": "record",
                "data": {
                    "file": item["id"],
                    "url": "https://multimedia.nt.qq.com.cn/benchmark/" + item["id"],
                },
            }
        else:
            # Bank v2 rows carry `quoted` (who originally said it) apart from the owner's own
            # words in `text`; v1 rows put everything in one third-party node.
            quoted = item.get("quoted") or {"text": item["text"], "uin": "10003",
                                            "name": "转发来源" if mode == "forward" else "旧约定引用"}
            source = {
                "type": "node",
                "data": {
                    "name": quoted.get("name") or "转发来源",
                    "uin": str(quoted.get("uin") or "10003"),
                    "content": [{"type": "text", "data": {"text": quoted["text"]}}],
                },
            }
            caption = (item["text"] if item.get("quoted") and item["text"]
                       else "这是一段转发或引用资料，请分清说话的人。")
        message = [{"type": "text", "data": {"text": caption}}, source] if caption else [source]
        frame = {
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "self_id": 10002,
            "user_id": 10001,
            "sender": {"user_id": 10001, "nickname": "合成人物"},
            "message_id": turn,
            "time": int(self.core.clock.now.timestamp()),
            "message": message,
        }
        event = normalize(
            encode(frame),
            self.bridge,
            now=self.core.clock.now.timestamp(),
            image_scope="owner_image_v1",
            file_scope="owner_file_v1",
        )
        await enrich(event, message, None, lambda t: t, image_scope=True, file_scope=True)
        admitted, event_id = self.ingest(
            event["payload"]["text"], f"turn-{turn:03}", payload=event["payload"]
        )
        core, s, observed = self.core, self.core.store, ""
        media_error = None
        try:
            if mode == "image":
                original = next(
                    r for r in event["payload"]["rich"]["segments"] if r["type"] == "image"
                )
                lease = agent_images.claim(
                    core.content, event_id, "image:0", original["source_sha256"]
                )
                data = (SUITE / item["asset"]).read_bytes()
                proof = {
                    "authority": self.authority,
                    "event_id": event_id,
                    "slot": 0,
                    "source_sha256": original["source_sha256"],
                    "lease_token": lease["lease_token"],
                    "media_sha256": hashlib.sha256(data).hexdigest(),
                    "mime": "image/png",
                }
                proof["signature"] = self.key.sign(image_upload_signing_payload(proof)).hex()
                agent_images.stage(
                    core.content,
                    event_id,
                    "image:0",
                    data,
                    "image/png",
                    lease["lease_token"],
                    proof,
                )
            elif mode == "file":
                original = next(
                    r for r in event["payload"]["rich"]["segments"] if r["type"] == "file"
                )
                lease = content_files.claim(
                    core.content, event_id, "file:0", original["source_sha256"]
                )
                proof = {
                    "authority": self.authority,
                    "event_id": event_id,
                    "slot": 0,
                    "source_sha256": original["source_sha256"],
                    "lease_token": lease["lease_token"],
                    "file_id": original["file_id"],
                    "filename": original["filename"],
                    "media_sha256": hashlib.sha256(data).hexdigest(),
                    "mime": "text/plain",
                    "bytes": len(data),
                }
                proof["signature"] = self.key.sign(file_upload_signing_payload(proof)).hex()
                content_files.stage_file(
                    core.content,
                    event_id,
                    "file:0",
                    data,
                    "text/plain",
                    lease["lease_token"],
                    proof,
                )
                await content_files.run_file_once(core.content, parse=parse_remote)
                row = s.one(
                    "SELECT state,output_event_id FROM content_items WHERE owner_event_id"
                    "=? AND kind='qq_file'",
                    (event_id,),
                )
                if row["state"] != "SUCCEEDED":
                    raise RuntimeError("FILE_WORKER_" + row["state"])
                parsed = s.one(
                    "SELECT body_json FROM events WHERE event_id=?", (row["output_event_id"],)
                )
                observed = encode(json.loads(parsed["body_json"]))
            elif mode in {"voice", "relative_voice"}:
                data = (SUITE / item["asset"]).read_bytes()
                asr = ASR[item["asset"]]
                assert asr["sha256"] == hashlib.sha256(data).hexdigest()
                proof = sign_asr_proof(
                    self.key,
                    authority=voice_authority.current(
                        s, core.settings, core.clock
                    ).authority.as_dict(),
                    event_id=event_id,
                    source_event_id=event["source_event_id"],
                    source_input_sha256=input_digest(event["payload"]),
                    source_segment_index=len(message) - 1,
                    audio_sha256=asr["sha256"],
                    audio_bytes=len(data),
                    source_partial=False,
                )
                with s.transaction():
                    voice_job = core.voice.submit_signed_asr(event_id, data, upload_proof=proof)
                jobs = core.voice.jobs
                session = jobs.session("local")
                claimed = jobs.claim(
                    "local",
                    VoiceClaimRequest(
                        node_id="local",
                        incarnation=session.incarnation,
                        sequence=session.last_sequence + 1,
                        manifest_sha256=core.settings.voice.nodes["local"].manifest_sha256,
                        resource_state="READY",
                    ),
                )
                ref = VoiceLeaseRef.from_lease(claimed.lease)
                assert claimed.lease.job_id == voice_job["job_id"]
                assert jobs.input("local", ref)[0] == data
                payload = VoiceTranscriptResult(
                    **{k: asr[k] for k in VoiceTranscriptResult.model_fields if k in asr}
                )
                value = make_voice_result(ref, payload)
                jobs.submit("local", value)
                jobs.control(
                    "local",
                    VoiceControlRequest(ref=ref, phase="RESULT_LOST", resource_state="READY"),
                )
                observed = asr["text"]
            else:
                observed = item["text"]
                if item.get("quoted"):
                    observed = (item["text"] + "\n" if item["text"] else "") + (
                        f"[{item['quoted'].get('name') or '转发来源'}] {item['quoted']['text']}")
        except Exception as error:
            media_error = getattr(error, "code", str(error))
            append(self.directory / "media-errors.jsonl", {"id": item["id"], "error": media_error})
        usage_before = {r["request_id"] for r in s.rows("SELECT request_id FROM model_usage")}
        await run_with_retries(core, "mixed-media-import", admitted["job_id"])
        state = s.one(
            "SELECT state,last_error_code FROM jobs WHERE job_id=?", (admitted["job_id"],)
        )
        usages = [
            r for r in s.rows("SELECT * FROM model_usage") if r["request_id"] not in usage_before
        ]
        skipped = state["state"] not in {"SUCCEEDED", "FAILED", "CANCELLED"} and (
            state["last_error_code"] in {"MODEL_RETRYABLE", "MODEL_OUTCOME_UNKNOWN"}
            and unknown_locally_closed(core, usages)
        )
        if skipped:
            # Keep the historical input/perception. The next native owner input
            # supersedes this chat job; never erase a corpus source to skip its reply.
            append(
                self.directory / "infrastructure-errors.jsonl",
                {
                    "id": item["id"],
                    "phase": "media_import",
                    "event_id": event_id,
                    "job_id": admitted["job_id"],
                    "native_state": state,
                    "usage": usages,
                },
            )
        elif state["state"] not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            save(
                self.directory / "pending-media-snapshot.json",
                {
                    "item": item,
                    "event_id": event_id,
                    "job_id": admitted["job_id"],
                    "native_state": state,
                    "observed_text": observed,
                    "media_error": media_error,
                },
            )
            raise RuntimeError(
                "NATIVE_MEDIA_NONTERMINAL:" + state["state"] + ":" + str(state["last_error_code"])
            )
        parts = s.rows("SELECT * FROM outbox WHERE origin_event_id=? ORDER BY rowid", (event_id,))
        reply = "\n".join(json.loads(r["content_json"]).get("text", "") for r in parts)
        with s.transaction():
            s.execute(
                "UPDATE outbox SET state='SENT',sent_at=?,updated_at=? WHERE origin_event_id=?",
                (core.clock(), core.clock(), event_id),
            )
        self.turn_ids.add(event_id)
        self.imported += 1
        append(
            self.directory / "turns-observed.jsonl",
            {
                **item,
                "observed_text": observed,
                "native_reply": reply,
                "native_state": state,
                "infrastructure_error": skipped,
                "media_error": media_error,
                "event_id": event_id,
            },
        )
        print(
            "media", self.directory.name, item["id"], mode, state["state"], media_error, flush=True
        )


async def run_profile(directory, persona, max_questions):
    directory.mkdir(parents=True, exist_ok=False)  # noqa: ASYNC240 -- local fixture setup
    cfg, auth = config(directory)
    history = [
        json.loads(s)
        for s in (SUITE / "inputs" / (persona + ".jsonl")).read_text("utf-8").splitlines()
    ]
    questions = sorted(
        (json.loads(s) for s in (SUITE / "questions.jsonl").read_text("utf-8").splitlines()),
        key=lambda q: (q.get("now", "9999"), q["id"]),
    )
    selected = set(CTX["selected_ids"])
    questions = [q for q in questions if q["id"] in selected and q["persona"] == persona]
    clock = Clock(history[0]["now"])
    migrate(cfg.database)
    holder = {}

    async def request(r):
        h = holder.get("h")
        append(
            directory / "requests.jsonl",
            {
                "case": h.case if h else None,
                "phase": h.phase if h else None,
                "logical_at": clock(),
                "actual_at": datetime.now(UTC).isoformat(),
                "payload": json.loads(r.content),
            },
        )

    async def response(r):
        await r.aread()
        h = holder.get("h")
        append(
            directory / "responses.jsonl",
            {
                "case": h.case if h else None,
                "phase": h.phase if h else None,
                "http_status": r.status_code,
                "response": r.json(),
            },
        )

    manifest = {
        "persona": persona,
        "corpus_manifest_sha256": hashlib.sha256(
            (SUITE / "manifest.json").read_bytes()
        ).hexdigest(),
        "core_commit": CTX["source_commit"],
        "model": cfg.model_name,
        "effective_model_settings": {
            name: getattr(cfg, name)
            for name in (
                "context_soft_tokens",
                "max_output_tokens",
                "memory_extract_max_output_tokens",
                "chat_thinking",
                "chat_thinking_tokens",
                "extraction_min_events",
                "extraction_idle_seconds",
                "raw_event_retention_days",
            )
        },
        "status": "STARTING",
        "questions": len(questions),
        "input_debounce": False,
        "short_term_turns": 6,
        "concurrency_basis": cfg.paid_model.concurrency_basis,
        "memory_extraction": "native default min5 idle900; native retry/checkpoint rules",
        "budget_cap_micro_usd": cfg.paid_model.window_micro_usd,
        "fx_validity_days": 366,
        "fx_hold_cny_per_usd": 10,
        "actual_started_at": datetime.now(UTC).isoformat(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),  # noqa: ASYNC240 -- frozen local script
    }
    save(directory / "manifest.json", manifest)
    async with httpx.AsyncClient(
        transport=MeteredTransport(
            BUDGET,
            lambda: (
                "inference:"
                + (CTX["campaign_arm"] + ":" if CTX.get("campaign_arm") else "")
                + persona
                + ":"
                + (holder["h"].phase if "h" in holder else "setup")
            ),
        ),
        timeout=cfg.timeout_seconds,
        trust_env=False,
        event_hooks={"request": [request], "response": [response]},
    ) as http:
        with Core(cfg, ModelClient(cfg, http), clock) as core:
            key = Ed25519PrivateKey.generate()
            purposes = ["chat", "memory", "history", "image", "file", "voice", "content"]
            policy = OwnerGrantPolicy(
                owner_qq_id="10001",
                purposes=purposes,
                authorization_sha256=auth,
                paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(cfg)),
                memory_scope="owner_private",
                history={
                    "scope": "owner_private",
                    "not_before": "2026-01-01T00:00:00+08:00",
                    # Same grant for both arms: room beyond the 6 recent pairs for
                    # source-bound recall (continuation or raw turns) under one ceiling.
                    "max_turns": CTX.get("history_grant", {}).get("max_turns", 14),
                    "max_utf8_bytes": CTX.get("history_grant", {}).get("max_utf8_bytes", 32000),
                },
                capability_policies={
                    k: v for k, v in owner_authority.capability_hashes(cfg).items() if k in purposes
                },
                capture={
                    "public_key": key.public_key().public_bytes_raw().hex(),
                    "identity_sha256": paid_budget.sha(["10001", "10002"]),
                    "protocol_epoch": "public-memory-benchmark-v1",
                },
            )
            query = AuthorityPreview(expected_revision=0, policy=policy)
            preview = owner_authority.preview(core.store, cfg, clock, query)
            authority = owner_authority.activate(
                core.store,
                cfg,
                clock,
                AuthorityChange(
                    operation_id="mixed-memory-activate",
                    **query.model_dump(),
                    preview_sha256=preview["preview_sha256"],
                ),
            )["authority"]
            h = Mixed(core, key, authority, directory)
            holder["h"] = h
            try:
                for q in questions:
                    if STOP.is_set():
                        raise RuntimeError("COHORT_INTERRUPTED_BY_OTHER_PROFILE")
                    while h.imported < q["cutoff_turn"]:
                        item = history[h.imported]
                        clock.set(item["now"])
                        await h.maintenance()
                        await h.import_source(item)
                    clock.set(q["now"])
                    await h.maintenance()
                    await h.exam(q)
                manifest["model_execution"] = execution_audit(core)
                if manifest["model_execution"]["unclosed_requests"]:
                    raise RuntimeError("NATIVE_MODEL_EXECUTION_UNCLOSED")
                manifest["status"] = "COMPLETED"
            except Exception as error:
                STOP.set()
                manifest.update(
                    status="INTERRUPTED",
                    error_type=type(error).__name__,
                    error_code=getattr(error, "code", str(error)),
                    case=h.case,
                    phase=h.phase,
                )
                print("interrupted", persona, manifest["error_code"], flush=True)
            finally:
                manifest.update(
                    imported_turns=h.imported,
                    actual_finished_at=datetime.now(UTC).isoformat(),
                    usage=core.store.rows(
                        "SELECT scenario,state,count(*) calls,sum(input_tokens) input_tokens,"
                        "sum(output_tokens) output_tokens,sum(billed_micro_usd) billed_micro_usd "
                        "FROM model_usage GROUP BY scenario,state"
                    ),
                )
                save(directory / "manifest.json", manifest)
    print("profile", persona, manifest["status"], flush=True)


async def main():
    directory = Path(CTX["run_dir"]) / "results"
    directory.mkdir(parents=True, exist_ok=False)  # noqa: ASYNC240 -- local fixture setup
    for persona in CTX["personas"]:
        await run_profile(directory / persona, persona, 0)


if __name__ == "__main__":
    asyncio.run(main())

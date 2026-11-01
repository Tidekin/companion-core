"""Deferred singing through the original SDK call and native voice queue."""

import json
from typing import Annotated

from pydantic import Field
from pydantic_ai import RunContext
from pydantic_ai.exceptions import CallDeferred
from pydantic_ai.toolsets import FunctionToolset

from companion import owner_authority, singing
from companion.agent_runtime import check_conversation
from companion.contracts import CoreError
from companion.voice_jobs import VoiceJobs
from companion.voice_protocol import digest


def result(store, settings, clock, job, request_id):
    jobs = VoiceJobs(store, settings, clock)
    row = jobs._get(request_id)
    if row["operation"] != "sing" or row["event_id"] != job["event_id"]:
        raise CoreError("SINGING_ORIGINAL_TOOL_REQUIRED", 403)
    status = jobs.status(request_id)
    if status["state"] in {"QUEUED", "LEASED", "RUNNING"}:
        return None
    if status["state"] != "READY":
        return {"request_id": request_id, "state": status["state"],
                "error_code": status["last_error_code"], "retry_allowed": False}
    _, recipe = singing.check_source(store, settings, clock, jobs._get(request_id))
    if recipe.chat_job_id != job["job_id"]:
        raise CoreError("SINGING_ORIGINAL_TOOL_REQUIRED", 403)
    manifest = jobs.singing_manifest(request_id)
    return {"request_id": request_id, "state": "READY", "manifest": manifest,
            "result_sha256": digest(manifest)}


def ready_result(core, job, snapshots, request_id):
    answer = result(core.store, core.settings, core.clock, job, request_id)
    if answer and answer["state"] == "READY":
        proof = {"kind": "singing", "purpose": "voice", "request_id": request_id,
                 "sha256": digest(answer)}
        if proof not in snapshots:
            snapshots.append(proof)
    return answer


def revalidate(store, settings, clock, job, snapshots):
    for proof in snapshots:
        if proof["kind"] == "singing":
            answer = result(store, settings, clock, job, proof["request_id"])
            if (proof.get("purpose") != "voice" or not answer or answer["state"] != "READY"
                or digest(answer) != proof["sha256"]):
                raise CoreError("SINGING_RESULT_CHANGED", 409)


def selected(store, settings, clock, job, request_id):
    saved = store.one("SELECT snapshots_json FROM agent_conversations WHERE job_id=?",
                       (job["job_id"],))
    proofs = json.loads(saved["snapshots_json"]) if saved else []
    if not any(p["kind"] == "singing" and p["request_id"] == request_id for p in proofs):
        raise CoreError("SINGING_READY_RESULT_REQUIRED", 403)
    revalidate(store, settings, clock, job, proofs)
    return VoiceJobs(store, settings, clock).singing_manifest(request_id)


def toolset(core, job, snapshots, persistence):
    async def sing_song(ctx: RunContext,
        source_event_id: Annotated[str, Field(min_length=1, max_length=128)],
        part: Annotated[int, Field(ge=1, le=100)] = 1) -> dict:
        """用固定本地音色翻唱一首真实 B 站歌曲。可按点歌或对话气氛自行选择。

        先用 reading_catalog 获取主人分享的链接，或 discover_content 按歌名搜索。
        source_event_id 必须是目录或 Reader 实际返回的事件，part 是实际视频分P。
        只支持十分钟内公开歌曲；不要重复调用失败或 UNKNOWN 的原任务。
        等待返回 READY 后，使用 reply_with_song 选择整曲，Core 按序发送语音气泡。
        """
        check_conversation(core, job, snapshots, purpose="voice")
        try:
            with core.store.transaction():
                status = core.voice.jobs.create_sing(job, source_event_id, part, ctx.tool_call_id)
                metadata = persistence.bind_request(ctx.tool_call_id, status["job_id"])
        except CoreError as error:
            if error.code not in {"SINGING_READ_BUDGET_UNAVAILABLE", "VOICE_CAPACITY_EXCEEDED",
                                  "SINGING_ORIGINAL_ATTEMPT_EXISTS",
                                  "SINGING_EXACT_VIDEO_REQUIRED"}:
                raise
            return {"state": "UNAVAILABLE", "error_code": error.code, "retry_allowed": False}
        raise CallDeferred(metadata=metadata)

    options = core.settings.voice.singing
    if not core.settings.voice.enabled or options is None:
        return None
    view = owner_authority.check_job(core.store, core.settings, core.clock, job, purpose="chat")
    if not {"voice", "content"} <= set(view.policy.purposes):
        return None
    singing.policy(core.settings)
    return FunctionToolset([sing_song], sequential=True, max_retries=0)

"""The private chat entry point; Core retains its job and final Outbox transaction."""

import json

from pydantic_ai import DeferredToolRequests

from companion import reply_revision
from companion.agent_persistence import ConversationStore
from companion.agent_reading import deferred_results, reader_tools
from companion.agent_runtime import check_conversation, run_conversation
from companion.agent_tools import core_query_tools
from companion.contracts import CoreError
from companion.paid_budget import sha
from companion.store import after
from companion.telemetry import emit, phase


async def reply(core, job, snapshots):
    with phase("context_guard", job["trace_id"]):
        view = check_conversation(core, job, snapshots)
    with phase("context_restore", job["trace_id"]):
        persistence = ConversationStore(core, job, snapshots)
        conversation = persistence.load()
        ready, answers = deferred_results(core, job, snapshots, conversation) if conversation else (
            True, None)
    if not ready:
        wait(core, job, persistence)
        return None, persistence
    with phase("context_tools", job["trace_id"]):
        tools = [core_query_tools(core, job, snapshots)]
        from companion.agent_singing import toolset

        if singing_tools := toolset(core, job, snapshots, persistence):
            tools.append(singing_tools)
        if (core.settings.content_enabled and core.settings.observation_enabled
            and "content" in view.policy.purposes):
            tools.append(reader_tools(core, job, snapshots, persistence))
        from companion import qq_video, video_input

        if video_input.enabled(core.settings) and {"content", "image"} <= set(view.policy.purposes):
            tools.append(qq_video.toolset(core, job, snapshots))
        from companion import image_generation

        if image_generation.available(core, job) and "expression" in view.policy.purposes:
            tools.append(image_generation.toolset(core, job, snapshots))
    with phase("context_build", job["trace_id"]):
        memories = []
        query = json.loads(job["body_json"]).get("text", "").strip()
        if (conversation is None and query and core.settings.memory_enabled
            and "memory" in view.policy.purposes):
            trace = {}
            try:
                result = await core.memory_context.recall_async(query, core.clock(),
                    project_query=query, clock=core.clock, trace=trace,
                    source_check=lambda: check_conversation(
                        core, job, snapshots, purpose="memory"))
            except CoreError as error:
                if error.code == "PRIVACY_CHANGED":
                    raise CoreError(reply_revision.CANCELLED, 409) from error
                raise
            memories = result["rows"]
            proof = core.memory_context.pin(core.clock(), rows=memories,
                                             event_ids=[job["event_id"]])
            reply_revision.remember_sources(core.store, job, proof["source_event_ids"])
            snapshots.append({"kind": "memory", "proof": proof, "retrieval": {
                "stage": "prefetch", "query_sha256": sha(query), "selection": trace}})
        messages = [] if conversation is not None else core.context(
            job, memories=memories, snapshots=snapshots,
            model_tools=tuple(name for toolset in tools for name in toolset.tools),
            planning=True,
        )
    emit("t_context_ready", job["trace_id"])
    result = await run_conversation(
        core, job, messages, toolsets=tools, conversation=conversation,
        deferred_tool_results=answers,
        snapshots=snapshots, persistence=persistence,
    )
    if isinstance(result.output, DeferredToolRequests):
        wait(core, job, persistence)
        return None, persistence
    from companion.agent_output import SongReply, VoiceReply

    output = (result.output.text if isinstance(result.output, (VoiceReply, SongReply))
              else result.output)
    return output, persistence


def wait(core, job, persistence):
    with core.store.transaction():
        persistence.check()
        core.store.execute(
            "UPDATE jobs SET state='RETRY_WAIT',lease_token=NULL,next_attempt_at=?,"
            "attempts=MAX(0,attempts-1),updated_at=? WHERE job_id=? AND lease_token=?",
            (after(core.clock(), 1), core.clock(), job["job_id"], job["lease_token"]),
        )

"""The private chat entry point; Core retains its job and final Outbox transaction."""

import asyncio
import json
import random
from time import monotonic

import psycopg
from pydantic_ai import DeferredToolRequests

from companion import job_queue, qq_video, reply_revision, retrieval_work, transport
from companion.agent_persistence import ConversationStore
from companion.agent_reading import deferred_results, reader_tools
from companion.agent_runtime import check_conversation, run_conversation
from companion.agent_tools import core_query_tools
from companion.contracts import CoreError
from companion.paid_budget import sha
from companion.retrieval import query_scope
from companion.store import after
from companion.telemetry import emit, phase


@query_scope()
async def reply(core, job, snapshots):
    with phase("context_restore", job["trace_id"]):
        deadline = monotonic() + core.settings.chat_restore_timeout_seconds
        attempt = 0
        while True:
            try:
                with core.store.deadline(deadline - monotonic()):
                    with phase("context_guard", job["trace_id"]):
                        view = check_conversation(core, job, snapshots)
                    persistence = ConversationStore(core, job, snapshots)
                    conversation = persistence.load()
                    saved = persistence.row()
                    if saved and saved["phase"] == "OUTPUT":
                        return saved["output_text"], persistence
                    ready, answers = (deferred_results(core, job, snapshots, conversation)
                                      if conversation else (True, None))
                if not ready:
                    wait(core, job, persistence)
                    return None, persistence
                break
            except (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected,
                    psycopg.errors.SerializationFailure, psycopg.errors.QueryCanceled,
                    retrieval_work.WorkLimit) as error:
                if isinstance(error, retrieval_work.WorkLimit) and error.reason not in {
                    "sql_milliseconds", "sql_busy",
                }:
                    raise
                if core.store.in_transaction:
                    raise
                # Restore only revalidates saved inputs and original Reader/voice
                # results. Model requests and tool execution remain outside retry.
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise CoreError("CONVERSATION_RESTORE_BUSY", 503) from error
                await asyncio.sleep(min(remaining, random.uniform(
                    .01, min(.25, .02 * 2 ** min(attempt, 4)))))
                attempt += 1
    with phase("context_tools", job["trace_id"]):
        text_only = transport.native_text(core.store, job["event_id"])
        tools = [core_query_tools(core, job, snapshots)]
        from companion.agent_singing import toolset

        if not text_only and (singing_tools := toolset(core, job, snapshots, persistence)):
            tools.append(singing_tools)
        if (core.settings.content_enabled and core.settings.observation_enabled
            and "content" in view.policy.purposes):
            tools.append(reader_tools(core, job, snapshots, persistence))
        from companion import video_input

        if (not text_only and video_input.enabled(core.settings)
                and {"content", "image"} <= set(view.policy.purposes)):
            tools.append(qq_video.toolset(core, job, snapshots))
        from companion import image_generation

        if (not text_only and "expression" in view.policy.purposes
                and image_generation.available(core, job)):
            tools.append(image_generation.toolset(core, job, snapshots))
    with phase("context_build", job["trace_id"]):
        memories = []
        query = json.loads(job["body_json"]).get("text", "").strip()
        if (conversation is None and query and core.settings.memory_enabled
            and "memory" in view.policy.purposes):
            trace = {}
            try:
                with phase("recall_memory", job["trace_id"]), retrieval_work.current().stage(
                    "automatic_memory", reserve={**retrieval_work.FINAL_SOURCE_RESERVE,
                        "candidates": 24,
                        "searches": 1}):
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
        ranking = None
        if (conversation is None and query and core.settings.memory_enabled
            and {"memory", "history"} <= set(view.policy.purposes)):
            from companion import turn_recall

            # Embedding and cross-encoder scoring run in a worker thread, not on the loop.
            with phase("recall_turns", job["trace_id"]), retrieval_work.current().stage(
                "automatic_turns", reserve=retrieval_work.FINAL_SOURCE_RESERVE,
            ):
                ranking = await turn_recall.prefetch(core.store, core.settings, job, query)
        messages = [] if conversation is not None else core.context(
            job, memories=memories, snapshots=snapshots,
            model_tools=tuple(name for toolset in tools for name in toolset.tools),
            planning=True, turn_ranking=ranking,
        )
    emit("t_context_ready", job["trace_id"])
    try:
        result = await run_conversation(
            core, job, messages, toolsets=tools, conversation=conversation,
            deferred_tool_results=answers,
            snapshots=snapshots, persistence=persistence,
        )
    except qq_video.PreviewReplyReady as preview:
        return str(preview), persistence
    if isinstance(result.output, DeferredToolRequests):
        wait(core, job, persistence)
        return None, persistence
    from companion.agent_output import final_text

    return final_text(result.output), persistence


def wait(core, job, persistence):
    with core.store.transaction():
        persistence.check()
        job_queue.retry_outcome(core.store, job["job_id"], "RETRY_WAIT", core.clock(),
                                after(core.clock(), 1), token=job["lease_token"], refund=True)

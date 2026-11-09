"""Core custody for SDK Conversation snapshots over the original job lease.

PostgreSQL owns snapshots and tool-to-Reader bindings. The original paid journal owns
money. An unresolved gap between those systems stops the step, never replays I/O.
The SDK remains the only model/tool loop; this module does not schedule effects.
"""

import json
from copy import deepcopy

from pydantic_ai import Conversation, DeferredToolRequests
from pydantic_ai.conversation import ConversationTypeAdapter
from pydantic_ai.exceptions import CallDeferred

from companion import context_planner, job_queue
from companion.agent_runtime import check_conversation
from companion.contracts import CoreError
from companion.privacy import redact
from companion.store import after, encode
from companion.transport import digest


def erase_conversations(store, event_ids):
    if not store.one('SELECT 1 FROM pg_catalog.pg_class WHERE '
        "relnamespace=current_schema()::regnamespace AND relname='agent_conversations' AND relkind "
        "IN ('r','p')"):
        return
    for event_id in event_ids:
        jobs = 'SELECT job_id FROM job_sources WHERE event_id=%s'
        store.execute("UPDATE agent_conversations SET phase='STOPPED',conversation_json=NULL,"
                      "snapshots_json='[]',output_text=NULL WHERE job_id IN (" + jobs + ")",
                      (event_id,))
        store.execute("UPDATE agent_tool_calls SET result_json=NULL,metadata_json=NULL,"
                      "args_sha256='',state=CASE WHEN state='STARTED' THEN 'UNKNOWN' ELSE state "
                      "END WHERE job_id IN (" + jobs + ")", (event_id,))


def recoverable(store, job_id):
    """Called by original job recovery; incomplete effects are never run a second time."""
    row = store.one('SELECT phase FROM agent_conversations WHERE job_id = %s', (job_id,))
    if row is None:
        return True
    if row["phase"] in {"MODEL_PENDING", "STOPPED", "COMPLETE"} or store.one(
        "SELECT 1 FROM agent_tool_calls WHERE job_id = %s AND state IN ('STARTED', 'UNKNOWN')",
        (job_id,),
    ):
        store.execute("UPDATE agent_tool_calls SET state = 'UNKNOWN' WHERE job_id = %s AND state = "
            "'STARTED'", (job_id,))
        store.execute("UPDATE agent_conversations SET phase = 'STOPPED' WHERE job_id = "
            '%s', (job_id,))
        return False
    return True


class ConversationStore:
    """A leased job's native Conversation and already-observed tool results."""

    def __init__(self, core, job, snapshots):
        self.core, self.job, self.snapshots = core, job, snapshots
        self.store = core.store
        self._image_inputs = None

    def check(self, *, current_request=None):
        check_conversation(self.core, self.job, self.snapshots, current_request=current_request)

    def row(self):
        return self.store.one('SELECT agent_conversations.job_id AS job_id, '
            'agent_conversations.phase AS phase, agent_conversations.conversation_json AS '
            'conversation_json, agent_conversations.snapshots_json AS snapshots_json, '
            'agent_conversations.request_id AS request_id, agent_conversations.output_text AS '
            'output_text, agent_conversations.created_at AS created_at, '
            'agent_conversations.updated_at AS updated_at FROM agent_conversations AS '
            'agent_conversations WHERE agent_conversations.job_id = %s',
                              (self.job["job_id"],))

    def load(self):
        row = self.row()
        if row is None:
            if self.store.one("SELECT 1 FROM model_usage WHERE job_id = %s AND scenario = 'chat'",
                              (self.job["job_id"],)):
                raise CoreError("CONVERSATION_OUTCOME_UNAVAILABLE", 409)
            return None
        if row["phase"] in {"MODEL_PENDING", "STOPPED", "COMPLETE"}:
            raise CoreError("CONVERSATION_OUTCOME_UNAVAILABLE", 409)
        self.snapshots[:] = json.loads(row["snapshots_json"])
        self.check()
        conversation = ConversationTypeAdapter.validate_json(row["conversation_json"])
        if conversation.conversation_id != self.job["job_id"]:
            raise CoreError("CONVERSATION_JOB_MISMATCH", 403)
        for message in conversation.messages:
            request_id = (message.metadata or {}).get("core_request_id")
            if request_id and not self.store.one(
                'SELECT 1 FROM model_usage WHERE request_id = %s AND job_id = %s AND state '
                "IN ('SETTLED','UNKNOWN')",
                (request_id, self.job["job_id"]),
            ):
                raise CoreError("MODEL_OUTCOME_UNKNOWN", 503)
        from companion.agent_images import restored_conversation

        return restored_conversation(self.core, self.job, conversation,
                                     originals=row["phase"] != "OUTPUT")

    def _save(self, conversation, phase, *, output=None):
        self.check()
        now = self.core.clock()
        if phase in {"MODEL_PENDING", "READY", "OUTPUT"} and self.store.in_write_transaction:
            job_queue.renew(self.store, self.job,
                            after(now, self.core.settings.lease_seconds), now=now)
        from companion.agent_images import inputs, stored_conversation

        if self._image_inputs is None:
            self._image_inputs = inputs(self.store, self.core.settings, self.core.clock, self.job)
        stored = stored_conversation(self.core, self.job, conversation, bindings=self._image_inputs)
        from pydantic_ai.messages import ModelResponse, ThinkingPart

        for message in stored.messages:
            if isinstance(message, ModelResponse):
                for part in message.parts:
                    if isinstance(part, ThinkingPart):
                        cleaned = redact(part.content, self.core.settings)
                        if cleaned != part.content:
                            details = message.provider_details or {}
                            message.provider_details = {**details, "reasoning": {
                                **details.get("reasoning", {}), "redacted": True}}
                        part.content = cleaned
        self.store.execute(
            'INSERT INTO agent_conversations (job_id, phase, conversation_json, snapshots_json, '
            'output_text, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s) ON '
            'CONFLICT(job_id) DO UPDATE SET phase = excluded.phase, conversation_json = '
            'excluded.conversation_json, snapshots_json = excluded.snapshots_json, output_text = '
            'excluded.output_text, updated_at = excluded.updated_at',
            (self.job["job_id"], phase,
             ConversationTypeAdapter.dump_json(stored).decode(), encode(self.snapshots),
             output, now, now),
        )

    async def before_model_request(self, ctx, request_context):
        with self.store.transaction():
            # This marker precedes reserve, so a process exit can never turn an
            # admitted-but-unrecorded attempt into permission for another request.
            self._save(Conversation(messages=deepcopy(list(request_context.messages)),
                                    usage=deepcopy(ctx.usage),
                                    conversation_id=self.job["job_id"]), "MODEL_PENDING")
            self.store.execute('UPDATE agent_conversations SET request_id = NULL WHERE job_id = %s',
                               (self.job["job_id"],))
        return request_context

    def admitted(self, request_id, messages):
        """Validate sources and wire bytes with registration in one native transaction."""
        from companion.agent_images import check_payload

        with self.store.transaction():
            self.check(current_request=request_id)
            check_payload(self.store, self.core.settings, self.core.clock, self.job, messages)
            context_planner.admitted(self.core, self.job, self.snapshots, request_id)
            self.store.execute('UPDATE agent_conversations SET request_id = %s, snapshots_json = '
                "%s WHERE job_id = %s AND phase = 'MODEL_PENDING'",
                               (request_id, encode(self.snapshots), self.job["job_id"]))

    def not_admitted(self):
        """A rejected reservation may retry only when no new chat attempt exists."""
        with self.store.transaction():
            row = self.row()
            if row is None or row["phase"] != "MODEL_PENDING":
                return
            conversation = ConversationTypeAdapter.validate_json(row["conversation_json"])
            known = {(message.metadata or {}).get("core_request_id")
                     for message in conversation.messages}
            actual = {item["request_id"] for item in self.store.rows(
                "SELECT request_id FROM model_usage WHERE job_id = %s AND scenario = 'chat'",
                (self.job["job_id"],),
            )}
            if actual <= known:
                context_planner.not_admitted(self.core, self.job, self.snapshots)
                self.store.execute("UPDATE agent_conversations SET phase = 'READY', snapshots_json "
                    '= %s WHERE job_id = %s', (encode(self.snapshots), self.job["job_id"]))

    async def after_model_request(self, ctx, *, request_context, response):
        with self.store.transaction():
            row = self.row()
            request_id = row["request_id"] if row else None
            if not request_id or not self.store.one(
                'SELECT 1 FROM model_usage WHERE request_id = %s AND job_id = %s AND state '
                "IN ('SETTLED','UNKNOWN')",
                (request_id, self.job["job_id"]),
            ):
                raise CoreError("MODEL_OUTCOME_UNKNOWN", 503)
            response.metadata = {**(response.metadata or {}), "core_request_id": request_id}
            # SDK 2.60 accounts response tokens before this hook, and increments
            # requests when committing its following graph node. The persisted
            # response already exists, so its checkpoint includes that request.
            usage = deepcopy(ctx.usage)
            usage.requests += 1
            # _save checks the live job and all source proofs in this same transaction,
            # immediately before persisting the received response.
            self._save(Conversation(messages=[*deepcopy(list(request_context.messages)), response],
                                    usage=usage,
                                    conversation_id=self.job["job_id"]), "READY")
        return response

    def failed_response(self, response):
        """Retain received diagnostics in the same checkpoint without allowing a replay."""
        from companion.agent_messages import to_runtime_response

        if not response.reasoning_content:
            return
        with self.store.transaction():
            row = self.row()
            if not row or not row["request_id"] or not row["conversation_json"]:
                return
            conversation = ConversationTypeAdapter.validate_json(row["conversation_json"])
            received = to_runtime_response(response, model_name=self.core.settings.model_name,
                                           provider_name="companion-controlled")
            received.metadata = {"core_request_id": row["request_id"]}
            conversation.messages.append(received)
            self._save(conversation, "STOPPED")

    async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
        key = self.job["job_id"], call.tool_call_id
        fingerprint = digest(args)
        with self.store.transaction():
            self.check()
            prior = self.store.one('SELECT agent_tool_calls.job_id AS job_id, '
                'agent_tool_calls.tool_call_id AS tool_call_id, agent_tool_calls.tool_name AS '
                'tool_name, agent_tool_calls.args_sha256 AS args_sha256, '
                'agent_tool_calls.model_request_id AS model_request_id, agent_tool_calls.state AS '
                'state, agent_tool_calls.result_json AS result_json, '
                'agent_tool_calls.metadata_json AS metadata_json, agent_tool_calls.request_id AS '
                'request_id, agent_tool_calls.created_at AS created_at, '
                'agent_tool_calls.updated_at AS updated_at, agent_tool_calls.voice_job_id AS '
                'voice_job_id FROM agent_tool_calls AS agent_tool_calls WHERE '
                'agent_tool_calls.job_id = %s AND agent_tool_calls.tool_call_id = %s', key)
            request_id = self.row()["request_id"]
            if prior:
                if (prior["tool_name"] != call.tool_name or prior["args_sha256"] != fingerprint
                    or prior["model_request_id"] != request_id):
                    raise CoreError("CONVERSATION_TOOL_ID_CONFLICT", 409)
                if prior["state"] == "RETURNED" and prior["result_json"] is not None:
                    return json.loads(prior["result_json"])
                if prior["state"] == "DEFERRED" and prior["metadata_json"] is not None:
                    raise CallDeferred(metadata=json.loads(prior["metadata_json"]))
                raise CoreError("CONVERSATION_TOOL_OUTCOME_UNKNOWN", 409)
            now = self.core.clock()
            self.store.insert("agent_tool_calls", job_id=key[0], tool_call_id=key[1],
                              tool_name=call.tool_name, args_sha256=fingerprint, state="STARTED",
                              model_request_id=request_id,
                              created_at=now, updated_at=now)
        try:
            result = await handler(args)
        except CallDeferred as deferred:
            with self.store.transaction():
                self.check()
                # Native effect handlers bind request_id in their own creation
                # transaction. A deferral without that binding is not recoverable.
                prior = self.store.one('SELECT state, metadata_json FROM agent_tool_calls WHERE '
                    'job_id = %s AND tool_call_id = %s', key)
                if (not prior or prior["state"] != "DEFERRED"
                    or json.loads(prior["metadata_json"]) != deferred.metadata):
                    raise CoreError("CONVERSATION_DEFERRED_BINDING_REQUIRED", 409) from deferred
            raise
        with self.store.transaction():
            self.check()
            self.store.execute("UPDATE agent_tool_calls SET state = 'RETURNED', result_json = %s, "
                'updated_at = %s WHERE job_id = %s AND tool_call_id = %s',
                               (encode(result), self.core.clock(), *key))
            self.store.execute('UPDATE agent_conversations SET snapshots_json = %s WHERE job_id = '
                '%s',
                               (encode(self.snapshots), self.job["job_id"]))
        return result

    def bind_request(self, call_id, request_id):
        """The native request creator calls this inside the same SQLite transaction."""
        if not self.store.in_transaction:
            raise RuntimeError("CONVERSATION_READER_TRANSACTION_REQUIRED")
        self.check()
        metadata = {"request_id": request_id}
        original = self.store.one('SELECT tool_name FROM agent_tool_calls WHERE job_id = %s AND '
            'tool_call_id = %s', (self.job["job_id"], call_id))
        if original is None:
            raise CoreError("CONVERSATION_DEFERRED_BINDING_REQUIRED", 409)
        column = "voice_job_id" if original["tool_name"] == "sing_song" else "request_id"
        changed = self.store.execute(
            f"UPDATE agent_tool_calls SET state='DEFERRED',{column}=%s,metadata_json=%s,"
            "updated_at=%s WHERE job_id=%s AND tool_call_id=%s AND state='STARTED'",
            (request_id, encode(metadata), self.core.clock(), self.job["job_id"], call_id),
        )
        if changed != 1:
            raise CoreError("CONVERSATION_DEFERRED_BINDING_REQUIRED", 409)
        return metadata

    def save_result(self, result):
        from companion.agent_output import LegacySongReply, MixedReply, save_format

        waiting = isinstance(result.output, DeferredToolRequests)
        with self.store.transaction():
            if isinstance(result.output, MixedReply):
                from companion.reply_composition import validate

                validate(self.core, self.job, result.output)
            elif isinstance(result.output, LegacySongReply):
                from companion.agent_singing import selected

                selected(self.store, self.core.settings, self.core.clock,
                         self.job, result.output.request_id)
            self._save(result.conversation, "WAITING" if waiting else "OUTPUT",
                       output=None if waiting else save_format(result.conversation, result.output))

    def complete(self):
        """Join the original job/Outbox commit, never create another delivery path."""
        if not self.store.in_transaction:
            raise RuntimeError("CONVERSATION_COMPLETION_TRANSACTION_REQUIRED")
        self.check()
        if self.store.execute("UPDATE agent_conversations SET phase = 'COMPLETE', updated_at = %s "
            "WHERE job_id = %s AND phase = 'OUTPUT'",
                              (self.core.clock(), self.job["job_id"])) != 1:
            raise CoreError("CONVERSATION_OUTPUT_REQUIRED", 409)

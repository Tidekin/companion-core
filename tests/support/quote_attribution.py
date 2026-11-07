from companion.contracts import InboundEvent
from companion.transport import enqueue
from tests.support.onebot_events import normalized, raw_event
from tests.support.onebot_reference_reader import enriched, reply, text

QUOTE = "这张图用了一个反差梗。"


QUESTION = "你刚才说的梗是什么意思？"


ORIGIN = "onebot-direct:FriendMessage:10001"


def quoted_job(core, quote=QUOTE, reference="-42", *, extra=None):
    core.settings.rich_input_scope = "references_v1"
    core.settings.max_recent_messages = 0
    incoming, _ = enriched([
        reply(reference, message=[text(quote)], **(extra or {})), text(QUESTION),
    ])
    event = InboundEvent.model_validate(incoming)
    core.ingest(event, event.event_id)
    job = core.store.one(
        "SELECT j.*,e.body_json,e.conversation_id FROM jobs j JOIN events e USING(event_id) "
        "WHERE j.event_id=?", (event.event_id,),
    )
    return job, incoming


def delivered(core, *, quote=QUOTE, origin=ORIGIN, state="SENT", number=1):
    incoming = normalized(raw_event(number, text="只应留在历史中的旧问题"))
    incoming["conversation"]["platform_session_key"] = origin
    incoming["source_event_id"] = incoming["source_event_id"].replace(ORIGIN, origin)
    event = InboundEvent.model_validate(incoming)
    saved = core.ingest(event, event.event_id)
    row = core.store.one("SELECT * FROM events WHERE event_id=?", (event.event_id,))
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?", (saved["job_id"],))
    message_id = enqueue(core.store, event.event_id, row["conversation_id"], quote, core.clock())
    core.store.execute("UPDATE outbox SET state=?,provider_message_id=?,sent_at=? "
                       "WHERE message_id=?", (state, "-42", core.clock(), message_id))
    return message_id

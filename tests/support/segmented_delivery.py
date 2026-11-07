from companion.contracts import DeliveryResult, LeaseRequest
from companion.transport import begin_send, delivery_result, enqueue_reply_parts, lease_deliveries
from tests.conftest import ingest

PARTS = ["今天先把手头的事情做好。", "然后我们再慢慢聊你的计划。", "剩下的事情可以明天继续。"]


def freeze(core, parts=PARTS, gap=1500):
    ingest(core, text="一起聊一聊接下来的计划")
    job = core.store.one("SELECT j.*,e.conversation_id FROM jobs j JOIN events e USING(event_id)")
    ids = enqueue_reply_parts(core.store, job, parts, gap, core.clock())
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?", (job["job_id"],))
    return job, ids


def lease(core, worker="segmented-test"):
    return lease_deliveries(
        core.store, core.settings, LeaseRequest(worker_id=worker, limit=5), core.clock()
    )["items"]


def acknowledge(core, item, result="SENT"):
    begin_send(core.store, item["message_id"], item["lease_token"], core.clock(), core.settings)
    delivery_result(
        core.store,
        core.settings,
        item["message_id"],
        DeliveryResult(
            lease_token=item["lease_token"],
            result=result,
            provider_message_id="100" if result == "SENT" else None,
        ),
        core.clock(),
    )

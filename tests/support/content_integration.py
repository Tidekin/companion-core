from companion.contracts import InboundEvent
from companion.exploration import ReadItem, ReadResult
from tests.conftest import event
from tests.support.content import POST


def share(core, number=1, text="", manifest=None):
    body = event(core, number, text).model_dump(mode="json")
    body["payload"]["shared_content"] = manifest or [{"kind": "link", "url": POST}]
    parsed = InboundEvent.model_validate(body)
    core.ingest(parsed, f"key-{number}")
    return parsed


def finish_read(core, text="作者讨论游戏关卡设计，有自己的观点。"):
    work = core.exploration.lease("reader")
    assert work
    return core.exploration.complete(
        "reader",
        work["request_id"],
        ReadResult(
            lease_token=work["lease_token"],
            status="SUCCESS",
            used_bytes=300,
            items=[ReadItem(title="讨论", text=text, url=POST)],
        ),
    )

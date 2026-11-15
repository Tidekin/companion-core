"""Independent workers share care quota; evidence reads retain native purpose bounds."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import care, care_tools, paid_budget
from companion.care_protocol import CarePlan
from companion.contracts import CoreError
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.postgres import Database
from companion.store import Store, after
from tests.support.care import caring as caring
from tests.support.care import owner_job, plan
from tests.support.owner_authority import activate
from tests.support.owner_authority_setup import prepare
from tests.test_owner_channel_history import move_to_native


def grant(core, tmp_path, *, history=False, proactive=True):
    policy = prepare(core, tmp_path)
    return activate(
        core,
        OwnerGrantPolicy.model_validate(
            {
                **policy.model_dump(),
                "capture": {
                    "public_key": Ed25519PrivateKey.generate()
                    .public_key()
                    .public_bytes_raw()
                    .hex(),
                    "identity_sha256": paid_budget.sha(
                        [core.settings.owner_qq_id, core.settings.bot_qq_id]
                    ),
                    "protocol_epoch": "care-boundaries",
                },
                "purposes": [
                    "chat",
                    *(["proactive"] if proactive else []),
                    *(["history"] if history else []),
                ],
                "history": {
                    "scope": "owner_private",
                    "not_before": after(core.clock(), -1),
                    "max_turns": 1,
                    "max_utf8_bytes": 40,
                }
                if history
                else {},
            }
        ),
    )


def test_two_independent_workers_cannot_exceed_daily_care_quota(caring, tmp_path):
    core = caring
    for number in range(1, 5):
        plan(core, number)
    core.clock.advance(3600)

    def flush():
        with Database(core.settings.database) as database, closing(Store(database, tmp_path)) as db:
            return care.flush(db, core.settings, core.clock())

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sum(workers.map(lambda _: flush(), range(4))) == 2
    assert (
        core.store.one(
            "SELECT count(*) AS n FROM jobs WHERE idempotency_key LIKE 'notice:care:%%'"
        )["n"]
        == 2
    )
    assert (
        core.store.one("SELECT count(*) AS n FROM care_plans WHERE reason_code='DAILY_LIMIT'")["n"]
        == 2
    )


@pytest.mark.parametrize("history", [False, True])
def test_recent_updates_obey_history_purpose_and_byte_turn_bounds(caring, tmp_path, history):
    core = caring
    _, old_job = owner_job(core, 1, "这句在授权历史起点之前。")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (old_job["job_id"],))
    core.clock.advance(2)
    grant(core, tmp_path, history=history)
    # Schema-level evidence fixture: actual signed ingress is covered in test_care_entry.
    core.clock.advance(60)
    from tests.conftest import event

    for number, text in [(2, "这是新消息"), (3, "最近的消息")]:
        value = event(core, number, text).model_dump(mode="json")
        core.store.insert(
            "events",
            event_id=value["event_id"],
            schema_version=1,
            source="qq.onebot",
            source_event_id=value["source_event_id"],
            conversation_id=core.store.one("SELECT conversation_id FROM conversations")[
                "conversation_id"
            ],
            actor_id="owner",
            event_type="message.received",
            occurred_at=core.clock(),
            ingested_at=core.clock(),
            privacy_scope="private",
            body_json='{"text":"' + text + '"}',
            trace_id=value["trace_id"],
            memory_processed=1,
        )
    updates = care.recent_owner(core.store, core.settings, core.clock())
    assert [row["text"] for row in updates] == (["最近的消息"] if history else [])
    core.store.execute(
        "UPDATE events SET body_json=%s WHERE event_id='evt_test_3'",
        ('{"text":"' + "很长的消息" * 100 + '"}',),
    )
    assert care.recent_owner(core.store, core.settings, core.clock()) == []


def test_care_tools_and_changes_require_existing_proactive_permission(caring, tmp_path):
    core = caring
    _, _, job, request = plan(core)
    grant(core, tmp_path, proactive=False)
    from companion import owner_authority

    view = owner_authority.current(core.store, core.settings, core.clock, purpose="chat")
    assert not care_tools.available(core, view)
    with pytest.raises(CoreError):
        care.change(core.store, core.settings, job, "denied", request, core.clock())
    assert not care.flush(core.store, core.settings, after(core.clock(), 3600))
    assert core.store.one("SELECT count(*) AS n FROM care_plans")["n"] == 1


@pytest.mark.parametrize("native_latest", [False, True])
def test_due_plan_uses_latest_private_owner_channel(caring, native_latest):
    core = caring
    plan(core)
    _, job = owner_job(core, 2, "今天看的书还不错。")
    native = move_to_native(core, 2)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (job["job_id"],))
    if not native_latest:
        _, job = owner_job(core, 3, "回到QQ继续聊。")
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (job["job_id"],))
    core.clock.advance(3600)
    assert care.flush(core.store, core.settings, core.clock())
    selected = core.store.one(
        "SELECT e.conversation_id FROM jobs j JOIN events e USING(event_id) WHERE j.kind='activity'"
    )["conversation_id"]
    assert (selected == native) is native_latest


def test_care_does_not_outlive_original_source_retention(caring):
    core = caring
    core.settings.raw_event_retention_days = 1
    _, job = owner_job(core, 1, "两天后去考试。")
    with pytest.raises(CoreError, match="CARE_TIME_OUTSIDE_RETENTION"):
        care.change(
            core.store,
            core.settings,
            job,
            "too-late",
            CarePlan(
                about="考试后关心", quote="两天后去考试", check_at=after(core.clock(), 2 * 86400)
            ),
            core.clock(),
        )
    assert not core.store.rows("SELECT * FROM care_plans")

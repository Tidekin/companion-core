"""Reuse is limited to one synchronous transaction, never the next send boundary."""

import json

import pytest

from companion import owner_authority, owner_delivery, transport
from companion.contracts import CoreError, LeaseRequest
from companion.owner_authority_protocol import AuthorityStop, OwnerSendCheck
from tests.conftest import run
from tests.support.native_segments import session


def receipt_rows(core):
    # Model reply receipts only; a final failure adds Core's fixed failure notice.
    return core.store.rows(
        (
            'SELECT key,value FROM runtime_meta WHERE key LIKE '
            "'owner_delivery:%%' AND (CASE WHEN value IS JSON THEN "
            "value::json #>> '{kind}' END) IS DISTINCT FROM 'failure_notice' "
            'ORDER BY key'
        ),
    )


def model_replies(core):
    return core.store.rows("SELECT * FROM outbox WHERE delivery_key NOT LIKE 'failure:%%'")


def one_failure_notice(core):
    rows = core.store.rows("SELECT content_json FROM outbox WHERE delivery_key LIKE 'failure:%%'")
    assert [json.loads(row["content_json"]) for row in rows] == [
        {"text": owner_delivery.FAILURE_TEXT}]


def count_calls(monkeypatch, module, name):
    original, calls = getattr(module, name), []

    def counted(*args, **kwargs):
        calls.append(None)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, counted)
    return calls


def test_recording_a_group_validates_once_and_retains_all_receipts(core, tmp_path, monkeypatch):
    original, recorded = owner_delivery.record_replies, []

    def record(store, settings, clock, job, ids):
        assert store.in_transaction
        with monkeypatch.context() as patch:
            sources = count_calls(patch, owner_delivery, "_sources")
            usage = count_calls(patch, owner_delivery, "_usage")
            groups = count_calls(patch, transport, "reply_group")
            original(store, settings, clock, job, ids)
            assert len(sources) == len(usage) == len(groups) == 1
        recorded.extend(receipt_rows(core))

    monkeypatch.setattr(owner_delivery, "record_replies", record)

    async def scenario():
        async with session(core, tmp_path,
                           reply="第一段合成回答。第二段合成回答。第三段合成回答。") as host:
            await host.admit()
            assert await core.run_once()
            rows = core.store.rows("SELECT message_id,authority_json FROM outbox "
                                   "ORDER BY segment_index")
            assert len(rows) == len(recorded) == 3
            assert receipt_rows(core) == recorded
            expected = host.view.authority.as_dict()
            assert all(json.loads(row["authority_json"]) == expected for row in rows)
            assert {json.loads(row["value"])["message_id"] for row in recorded} == {
                row["message_id"] for row in rows}
            assert len(host.calls) == 1
            assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    run(scenario())


@pytest.mark.parametrize("invalid", ["incomplete", "reversed", "duplicate"])
def test_recording_requires_the_entire_ordered_group(core, tmp_path, monkeypatch, invalid):
    original, errors = owner_delivery.record_replies, []

    def record(store, settings, clock, job, ids):
        assert store.in_transaction and len(ids) == 2
        supplied = {"incomplete": ids[:1], "reversed": ids[::-1], "duplicate": ids * 2}[invalid]
        try:
            original(store, settings, clock, job, supplied)
        except CoreError as error:
            errors.append(error.code)
            raise

    monkeypatch.setattr(owner_delivery, "record_replies", record)

    async def scenario():
        async with session(core, tmp_path) as host:
            await host.admit()
            assert await core.run_once()
            assert errors == ["OWNER_DELIVERY_GROUP_CHANGED"]
            assert receipt_rows(core) == []
            assert not model_replies(core)
            one_failure_notice(core)
            assert len(host.calls) == 1 and host.sends == []
            usage = core.store.one("SELECT state,billed_micro_usd FROM model_usage")
            assert usage["state"] == "SETTLED" and usage["billed_micro_usd"] > 0
    run(scenario())


def test_recording_failure_rolls_back_all_receipts(core, tmp_path, monkeypatch):
    insert, recorded = core.store.insert, []

    def fail_second(table, **values):
        if table == "runtime_meta" and values.get("key", "").startswith(owner_delivery.DELIVERY):
            recorded.append(values["key"])
            if len(recorded) == 2:
                raise RuntimeError("synthetic receipt failure")
        return insert(table, **values)

    monkeypatch.setattr(core.store, "insert", fail_second)

    async def scenario():
        async with session(core, tmp_path) as host:
            await host.admit()
            assert await core.run_once()
            assert len(recorded) == 3  # two rolled-back replies, then the failure notice
            assert receipt_rows(core) == []
            assert not model_replies(core)
            one_failure_notice(core)
            assert len(host.calls) == 1 and host.sends == []
            usage = core.store.one("SELECT state,billed_micro_usd FROM model_usage")
            assert usage["state"] == "SETTLED" and usage["billed_micro_usd"] > 0
    run(scenario())


@pytest.mark.parametrize(("change", "error"), [
    ("runtime", "OWNER_AUTHORITY_PAID_POLICY_CHANGED"),
    ("stop", "OWNER_AUTHORITY_DISABLED"),
    ("receipt", "OWNER_DELIVERY_SOURCE_CHANGED"),
    ("lease", "OWNER_DELIVERY_LEASE_LOST"),
])


def test_each_send_boundary_rechecks_current_state(core, tmp_path, monkeypatch, change, error):
    async def scenario():
        async with session(core, tmp_path) as host:
            job = await host.admit()
            assert await core.run_once()
            ref = host.view.authority.as_dict()
            item = transport.lease_deliveries(core.store, core.settings,
                LeaseRequest(worker_id="reuse-test", limit=1, authority=ref),
                core.clock())["items"][0]
            sources = count_calls(monkeypatch, owner_delivery, "_sources")
            transport.begin_send(core.store, item["message_id"], item["lease_token"],
                core.clock(), core.settings, clock=core.clock)
            assert len(sources) == 1
            check = OwnerSendCheck(authority=ref, event_id=job["event_id"],
                message_id=item["message_id"], content_sha256=item["content_sha256"],
                lease_token=item["lease_token"], reply_revision=item["reply_revision"])
            assert owner_delivery.send_check(
                core.store, core.settings, core.clock, check)["approved"]
            assert len(sources) == 2
            usage = core.store.rows("SELECT * FROM model_usage")
            if change == "runtime":
                core.settings.paid_model = core.settings.paid_model.model_copy(
                    update={"window_calls": core.settings.paid_model.window_calls + 1})
            elif change == "stop":
                owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                    operation_id="stop-native-validation-reuse", authority=ref))
            elif change == "receipt":
                saved = core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                    (owner_delivery.DELIVERY + item["message_id"],))
                value = json.loads(saved["value"]) | {"content_sha256": "0" * 64}
                core.store.execute('UPDATE runtime_meta SET value=%s WHERE key=%s',
                    (json.dumps(value), owner_delivery.DELIVERY + item["message_id"]))
            else:
                core.clock.advance(core.settings.lease_seconds + 1)
            with pytest.raises(CoreError, match=error):
                owner_delivery.send_check(core.store, core.settings, core.clock, check)
            assert len(sources) == 3
            assert core.store.one('SELECT state FROM outbox WHERE message_id=%s',
                                  (item["message_id"],))["state"] == "SENDING"
            assert core.store.rows("SELECT * FROM model_usage") == usage
            assert host.sends == []
    run(scenario())

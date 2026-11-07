"""Real consume HTTP and independent native Life process preserve original wording."""

import asyncio
import json
import os
import subprocess
import sys

import httpx
import pytest
from pydantic import SecretStr

from companion.reply_segments import SegmentedReplyConfig
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.social_owner_authority import stop
from tests.test_life_expression_consumption import prepared, request


@pytest.fixture(autouse=True)
def close_provider_clients(core):
    core.authority_test_clients = []
    yield
    for client in core.authority_test_clients:
        run(client.aclose())


async def native_child(core, tmp_path, source, destination, number, *, wording=None):
    config = tmp_path / f"life-{number}.json"
    result = tmp_path / f"life-{number}-result.json"
    config.write_text(json.dumps({
        "settings": core.settings.model_dump(mode="python"), "now": core.clock(),
        "source_id": source, "destination": destination, "result_path": str(result),
        "wording": wording,
    }, default=lambda value: value.get_secret_value()
       if isinstance(value, SecretStr) else str(value)), encoding="utf-8")
    process = await asyncio.to_thread(subprocess.run,
        [sys.executable, "-B", "-m", "tests.life_expression_process", str(config)],
        capture_output=True, timeout=60)
    assert process.returncode == 0, process.stderr.decode(errors="replace")
    outcome = json.loads(result.read_text(encoding="utf-8"))
    assert outcome["pid"] != os.getpid()
    return outcome


@pytest.mark.parametrize("outcome", ["SENT", "ERASE", "UNKNOWN"])
def test_segmented_life_draft_actual_http_and_independent_worker(core, tmp_path, outcome):
    core.settings.segmented_reply = SegmentedReplyConfig(
        mode="semantic", max_segments="unlimited", interval_ms=600)
    _, _, authority, source = prepared(core, tmp_path,
                                       destination="PRIVATE_MESSAGE_DRAFT", execute=False)
    core.settings.control_token = SecretStr("synthetic-owner-life-consumption-control")
    texts = ["恒星的颜色真有意思。\n\n", "想再看看光谱。\n\n", "温度的关系也值得聊。"]

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
        ) as client:
            observed = await native_child(core, tmp_path, source, "PRIVATE_MESSAGE_DRAFT", 1,
                                           wording="".join(texts))
            assert observed["provider_calls"] == 1
            result = next(row for row in observed["results"] if row["state"] == "SHADOW")
            job_id = result["job_id"]
            body = request(core, job_id, authority, "qq.private.proactive").model_dump(mode="json")
            client.headers["Authorization"] = (
                "Bearer " + core.settings.control_token.get_secret_value())
            path = f"/internal/v2/life/results/{job_id}/consume"
            consumed = await client.post(path, json=body)
            assert consumed.status_code == 200, consumed.text
            assert len(consumed.json()["deliveries"]) == 3
            assert (await client.post(path, json=body)).json() == consumed.json()
            usage = core.store.rows("SELECT * FROM model_usage")
            assert len(usage) == 1 and usage[0]["state"] == "SETTLED"
            restarted = await native_child(core, tmp_path, source, "PRIVATE_MESSAGE_DRAFT", 2)
            assert restarted["provider_calls"] == 0
            client.headers["Authorization"] = (
                "Bearer " + core.settings.bridge_token.get_secret_value())
            lease_body = {"worker_id": "life-segments", "authority": authority}
            for index, text in enumerate(texts):
                leased = await client.post("/internal/v1/deliveries/lease", json=lease_body)
                assert leased.status_code == 200, leased.text
                items = leased.json()["items"]
                if index and outcome != "SENT":
                    assert items == []
                    break
                assert len(items) == 1 and items[0]["content"] == {"text": text}
                item = items[0]
                delivery_path = "/internal/v1/deliveries/" + item["message_id"]
                begun = await client.post(delivery_path + "/begin-send",
                                          json={"lease_token": item["lease_token"]})
                assert begun.status_code == 200, begun.text
                checked = await client.post("/internal/v1/owner-authority/send-check", json={
                    "authority": authority, "event_id": item["origin_event_id"],
                    "message_id": item["message_id"], "lease_token": item["lease_token"],
                    "content_sha256": item["content_sha256"]})
                assert checked.status_code == 200, checked.text
                ack = await client.post(delivery_path + "/result", json={
                    "lease_token": item["lease_token"],
                    "result": "DELIVERY_UNCERTAIN" if outcome == "UNKNOWN" else "SENT"})
                assert ack.status_code == 200, ack.text
                if outcome == "ERASE":
                    with core.store.transaction():
                        core.ledger.expire_events(core.store, [source], core.clock())
                if index < 2:
                    waiting = await client.post("/internal/v1/deliveries/lease", json=lease_body)
                    if outcome == "ERASE":
                        # Erasing an approved source revokes the native grant as well.
                        assert waiting.status_code == 403, waiting.text
                        break
                    assert waiting.status_code == 200 and waiting.json()["items"] == []
                    core.clock.advance(0.6)
            states = [row["state"] for row in core.store.rows(
                "SELECT state FROM outbox ORDER BY segment_index")]
            assert states == (["SENT"] * 3 if outcome == "SENT" else
                ["DELIVERY_UNCERTAIN" if outcome == "UNKNOWN" else "SENT",
                 "CANCELLED", "CANCELLED"])
            assert core.store.rows("SELECT * FROM model_usage") == usage

    run(scenario())


@pytest.mark.parametrize("action,destination", [
    ("qq.private.proactive", "PRIVATE_MESSAGE_DRAFT"), ("qzone.publish", "QZONE_DRAFT"),
])
@pytest.mark.parametrize("outcome", ["SENT", "UNKNOWN", "STOP", "ERASE"])
def test_native_life_result_consumed_once_over_actual_http(core, tmp_path, action, destination,
                                                        outcome):
    _, _, authority, source = prepared(core, tmp_path, destination=destination, execute=False)
    core.settings.control_token = SecretStr("synthetic-owner-life-consumption-control")

    async def child(number):
        return await native_child(core, tmp_path, source, destination, number)

    async def scenario():
        async with real_core_http(core) as port:
            first = await child(1)
            assert first["provider_calls"] == 1, first
            rows = [row for row in first["results"] if row["state"] == "SHADOW"]
            assert len(rows) == 1, first
            job_id = rows[0]["job_id"]
            body = request(core, job_id, authority, action).model_dump(mode="json")
            usage = core.store.rows("SELECT * FROM model_usage")
            assert len(usage) == 1 and usage[0]["state"] == "SETTLED"
            path = f"/internal/v2/life/results/{job_id}/consume"
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                                          trust_env=False) as client:
                assert (await client.post(path, json=body)).status_code == 401
                client.headers["Authorization"] = (
                    "Bearer " + core.settings.control_token.get_secret_value())
                changed = await client.post(path,
                    json={**body, "expected_content_sha256": "0" * 64})
                assert changed.status_code == 409, changed.text
                assert core.store.rows("SELECT * FROM outbox") == []
                consumed = await client.post(path, json=body)
                assert consumed.status_code == 200, consumed.text
                assert consumed.headers["Cache-Control"] == "no-store"
                repeated = await client.post(path, json=body)
                assert repeated.status_code == 200 and repeated.json() == consumed.json()
                outbox = core.store.rows("SELECT * FROM outbox")
                assert len(outbox) == 1
                content = json.loads(outbox[0]["content_json"])
                assert (content["proposal"]["text"] if action == "qzone.publish"
                        else content["text"]) == rows[0]["text"]
                if action == "qzone.publish":
                    assert content["audience"] == {"ugc_right": 16, "target_uins": ["10001"]}
                second = await child(2)
                assert second["provider_calls"] == 0, second
                assert core.store.rows("SELECT * FROM model_usage") == usage
                assert core.store.rows("SELECT * FROM outbox") == outbox
                assert (await client.post(path, json=body)).json() == consumed.json()
                client.headers["Authorization"] = (
                    "Bearer " + core.settings.bridge_token.get_secret_value())
                lease_body = {"worker_id": "synthetic-life-delivery", "authority": authority}
                if action == "qzone.publish":
                    lease_body.update(delivery_lane="qq_account",
                        account_actions_authorization_sha256=(
                            core.settings.social_autonomy.authorization_sha256))
                leased = await client.post("/internal/v1/deliveries/lease", json=lease_body)
                assert leased.status_code == 200, leased.text
                item = leased.json()["items"][0]
                delivery_path = "/internal/v1/deliveries/" + item["message_id"]
                if outcome == "STOP":
                    stop(core, authority)
                elif outcome == "ERASE":
                    with core.store.transaction():
                        core.ledger.expire_events(core.store, [source], core.clock())
                begun = await client.post(delivery_path + "/begin-send",
                                          json={"lease_token": item["lease_token"]})
                if outcome in {"STOP", "ERASE"}:
                    assert begun.status_code >= 400, begun.text
                    assert core.store.one("SELECT state FROM outbox")["state"] == "CANCELLED"
                else:
                    assert begun.status_code == 200, begun.text
                    result = {"lease_token": item["lease_token"],
                              "result": "SENT" if outcome == "SENT" else "DELIVERY_UNCERTAIN"}
                    if action == "qzone.publish":
                        checked = await client.post("/internal/v1/account-actions/send-check",
                            json={"authority": authority, "message_id": item["message_id"],
                                  "lease_token": item["lease_token"],
                                  "content_sha256": item["content_sha256"],
                                  "payload_sha256": content["payload_sha256"]})
                        assert checked.status_code == 200, checked.text
                        result["account_action_receipt"] = {
                            "message_id": item["message_id"], "lease_token": item["lease_token"],
                            "payload_sha256": content["payload_sha256"], "action_type": action,
                            "result": "SUCCEEDED" if outcome == "SENT" else "UNKNOWN",
                            "native_retcode": 0 if outcome == "SENT" else None,
                            "native_receipt_sha256": "e" * 64 if outcome == "SENT" else None,
                            "resource_id": "synthetic-life-tid" if outcome == "SENT" else None,
                        }
                    else:
                        checked = await client.post("/internal/v1/owner-authority/send-check",
                            json={"authority": authority,
                                  "event_id": outbox[0]["origin_event_id"],
                                  "message_id": item["message_id"],
                                  "lease_token": item["lease_token"],
                                  "content_sha256": item["content_sha256"]})
                        assert checked.status_code == 200, checked.text
                    recorded = await client.post(delivery_path + "/result", json=result)
                    assert recorded.status_code == 200, recorded.text
                    repeated = await client.post(delivery_path + "/result", json=result)
                    assert repeated.status_code == 200 and repeated.json() == recorded.json()
                    assert core.store.one("SELECT state FROM outbox")["state"] == result["result"]
                assert core.store.rows("SELECT * FROM model_usage") == usage
                assert len(core.store.rows("SELECT * FROM outbox")) == 1
    run(scenario())


@pytest.mark.parametrize("change", ["repeat", "correction", "delete", "erase", "stop"])
def test_native_life_diary_save_over_http_preserves_original_custody(core, tmp_path, change):
    core.settings.diary_enabled = True
    _, _, authority, source = prepared(core, tmp_path, destination="PRIVATE_DIARY",
                                      purposes=["diary"], execute=False)
    core.settings.control_token = SecretStr("synthetic-owner-diary-consumption-control")
    core.settings.diary_admin_token = SecretStr("synthetic-diary-admin-consumption-control")

    async def scenario():
        async with real_core_http(core) as port:
            first = await native_child(core, tmp_path, source, "PRIVATE_DIARY", 1)
            assert first["provider_calls"] == 1, first
            rows = [row for row in first["results"] if row["state"] == "SHADOW"]
            assert len(rows) == 1, first
            job_id = rows[0]["job_id"]
            body = request(core, job_id, authority, "diary.save").model_dump(mode="json")
            usage = core.store.rows("SELECT * FROM model_usage")
            assert len(usage) == 1 and usage[0]["state"] == "SETTLED"
            path = f"/internal/v2/life/results/{job_id}/consume"
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                headers={"Authorization": "Bearer "
                         + core.settings.control_token.get_secret_value()}) as client:
                saved = await client.post(path, json=body)
                assert saved.status_code == 200, saved.text
                receipt = saved.json()
                assert receipt["source_job_id"] == job_id and not receipt["reused"]
                entry = core.store.one("SELECT * FROM diary_entries")
                assert entry["body_md"] == rows[0]["text"] and entry["version"] == 1
                assert source in {r["event_id"] for r in core.store.rows(
                    "SELECT event_id FROM diary_evidence")}
                repeated = await client.post(path, json=body)
                assert repeated.status_code == 200 and repeated.json() == {
                    **receipt, "reused": True,
                }
                second = await native_child(core, tmp_path, source, "PRIVATE_DIARY", 2)
                assert second["provider_calls"] == 0, second
                diary_path = "/internal/v1/diary/" + receipt["local_date"]
                diary_headers = {"Authorization": "Bearer "
                                 + core.settings.diary_admin_token.get_secret_value()}
                if change == "correction":
                    corrected = await client.post(diary_path + "/correction",
                        json={"text": "这是主人保留的修订。"}, headers=diary_headers)
                    assert corrected.status_code == 200, corrected.text
                elif change == "delete":
                    deleted = await client.post(diary_path + "/forget", headers=diary_headers)
                    assert deleted.status_code == 200, deleted.text
                elif change == "erase":
                    with core.store.transaction():
                        core.ledger.expire_events(core.store, [source], core.clock())
                elif change == "stop":
                    stop(core, authority)
                before_retry = core.store.rows("SELECT * FROM diary_entries")
                retry = await client.post(path, json=body)
                if change in {"repeat", "correction"}:
                    assert retry.status_code == 200, retry.text
                    assert retry.json()["entry_id"] == receipt["entry_id"]
                    assert retry.json()["current_version"] == (2 if change == "correction" else 1)
                else:
                    assert retry.status_code >= 400, retry.text
                assert core.store.rows("SELECT * FROM diary_entries") == before_retry
                assert core.store.rows("SELECT * FROM model_usage") == usage
                assert core.store.rows("SELECT * FROM outbox") == []
    run(scenario())


@pytest.mark.parametrize("action,destination,purposes,outcome", [
    ("qq.private.proactive", "PRIVATE_MESSAGE_DRAFT", ["proactive"], "SENT"),
    ("qq.private.proactive", "PRIVATE_MESSAGE_DRAFT", ["proactive"], "UNKNOWN"),
    ("qzone.publish", "QZONE_DRAFT", ["social", "account_actions"], "SENT"),
    ("qzone.publish", "QZONE_DRAFT", ["social", "account_actions"], "UNKNOWN"),
    ("diary.save", "PRIVATE_DIARY", ["diary"], "SAVED"),
])
def test_native_worker_automatically_consumes_only_explicit_channel_consent(
    core, tmp_path, action, destination, purposes, outcome,
):
    core.settings.diary_enabled = True
    _, _, authority, source = prepared(core, tmp_path, destination=destination, purposes=purposes,
                                       automatic_actions=[action], execute=False)
    core.settings.control_token = SecretStr("synthetic-automatic-life-control-token")
    core.settings.diary_admin_token = SecretStr("synthetic-automatic-life-diary-token")

    async def scenario():
        async with real_core_http(core) as port:
            first = await native_child(core, tmp_path, source, destination, 1)
            assert first["provider_calls"] == 1, first
            rows = [row for row in first["results"] if row["state"] == "SHADOW"]
            assert len(rows) == 1, first
            usage = core.store.rows("SELECT * FROM model_usage")
            assert len(usage) == 1 and usage[0]["state"] == "SETTLED"
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                                          trust_env=False) as client:
                # No consume POST: the sole native selector must have done the work.
                if action == "diary.save":
                    entry = core.store.one("SELECT * FROM diary_entries")
                    assert entry and entry["body_md"] == rows[0]["text"]
                    read = await client.get("/internal/v1/diary/" + entry["local_date"],
                        headers={"Authorization": "Bearer "
                                 + core.settings.diary_admin_token.get_secret_value()})
                    assert read.status_code == 200, read.text
                    assert core.store.rows("SELECT * FROM outbox") == []
                else:
                    outbox = core.store.rows("SELECT * FROM outbox")
                    assert len(outbox) == 1
                    content = json.loads(outbox[0]["content_json"])
                    assert (content["proposal"]["text"] if action == "qzone.publish"
                            else content["text"]) == rows[0]["text"]
                    client.headers["Authorization"] = (
                        "Bearer " + core.settings.bridge_token.get_secret_value())
                    lease_body = {"worker_id": "synthetic-auto-bridge", "authority": authority}
                    if action == "qzone.publish":
                        lease_body.update(delivery_lane="qq_account",
                            account_actions_authorization_sha256=(
                                core.settings.social_autonomy.authorization_sha256))
                    leased = await client.post("/internal/v1/deliveries/lease", json=lease_body)
                    assert leased.status_code == 200, leased.text
                    item = leased.json()["items"][0]
                    path = "/internal/v1/deliveries/" + item["message_id"]
                    begun = await client.post(path + "/begin-send",
                                              json={"lease_token": item["lease_token"]})
                    assert begun.status_code == 200, begun.text
                    check = {"authority": authority, "message_id": item["message_id"],
                             "lease_token": item["lease_token"],
                             "content_sha256": item["content_sha256"]}
                    result = {"lease_token": item["lease_token"],
                              "result": "SENT" if outcome == "SENT" else "DELIVERY_UNCERTAIN"}
                    if action == "qzone.publish":
                        assert content["audience"] == {"ugc_right": 16, "target_uins": ["10001"]}
                        check["payload_sha256"] = content["payload_sha256"]
                        check_path = "/internal/v1/account-actions/send-check"
                        result["account_action_receipt"] = {
                            "message_id": item["message_id"], "lease_token": item["lease_token"],
                            "payload_sha256": content["payload_sha256"], "action_type": action,
                            "result": "SUCCEEDED" if outcome == "SENT" else "UNKNOWN",
                            "native_retcode": 0 if outcome == "SENT" else None,
                            "native_receipt_sha256": "d" * 64 if outcome == "SENT" else None,
                            "resource_id": "synthetic-auto-tid" if outcome == "SENT" else None,
                        }
                    else:
                        check["event_id"] = outbox[0]["origin_event_id"]
                        check_path = "/internal/v1/owner-authority/send-check"
                    checked = await client.post(check_path, json=check)
                    assert checked.status_code == 200, checked.text
                    recorded = await client.post(path + "/result", json=result)
                    assert recorded.status_code == 200, recorded.text
                    assert core.store.one("SELECT state FROM outbox")["state"] == result["result"]
                deliveries = core.store.rows("SELECT * FROM outbox")
                entries = core.store.rows("SELECT * FROM diary_entries")
                second = await native_child(core, tmp_path, source, destination, 2)
                assert second["provider_calls"] == 0, second
                assert core.store.rows("SELECT * FROM outbox") == deliveries
                assert core.store.rows("SELECT * FROM diary_entries") == entries
                assert core.store.rows("SELECT * FROM model_usage") == usage
    run(scenario())

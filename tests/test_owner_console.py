"""Owner browser access, durable edits and private read projections through HTTP."""

import json

import pytest
from pydantic import SecretStr, ValidationError

from companion import operator_config
from companion.contracts import Candidate
from companion.store import after, encode
from tests.conftest import ingest, run
from tests.support.owner_console import ORIGIN, PASSWORD, login, stored_memory
from tests.support.owner_console import client as client


def test_console_requires_its_own_owner_session_origin_and_csrf(client, core):
    assert client.get("/v1/console/overview").status_code == 401
    assert client.get("/v1/console/overview", headers={
        "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value(),
    }).status_code == 401
    assert client.post("/v1/console/session", json={"password": PASSWORD},
                       headers={"Origin": "https://attacker.example"}).status_code == 403
    response = client.post("/v1/console/session", json={"password": "wrong"},
                           headers={"Origin": ORIGIN})
    assert response.status_code == 401 and "wrong" not in response.text
    headers = login(client)
    assert client.get("/v1/console/session").json()["csrf_token"] == headers["X-CSRF-Token"]
    assert client.post("/v1/console/mode", json={"target": "life", "enabled": False},
                       headers={"Origin": ORIGIN}).status_code == 403
    assert client.get("/v1/console/configuration").headers["cache-control"] == "no-store"
    assert client.delete("/v1/console/session", headers=headers).status_code == 200
    assert client.get("/v1/console/configuration").status_code == 401


def test_overview_reports_the_current_pause_state_the_mode_dialog_changes(client, core):
    headers = login(client)
    client.app_core.settings.proactive_enabled = True
    modes = client.get("/v1/console/overview").json()["runtime"]["modes"]
    assert {k: v["paused"] for k, v in modes.items()} == {
        "proactive": False, "life": False, "diary": False}
    response = client.post("/v1/console/mode", json={"target": "life", "enabled": False},
                           headers=headers)
    assert response.status_code == 200, response.text
    modes = client.get("/v1/console/overview").json()["runtime"]["modes"]
    assert modes["life"] == {"paused": True, "available": True}
    assert modes["proactive"]["paused"] is False
    client.app_core.settings.diary_enabled = False
    assert client.get("/v1/console/overview").json()["runtime"]["modes"]["diary"][
        "available"] is False


def test_empty_views_report_real_empty_and_unobserved_states(client):
    login(client)
    for view in ("activity", "memories", "life", "content", "memes", "diagnostics"):
        response = client.get("/v1/console/" + view)
        assert response.status_code == 200, (view, response.text)
        assert isinstance(response.json()["items"], list)
    overview = client.get("/v1/console/overview")
    assert overview.status_code == 200, overview.text
    value = overview.json()
    assert value["runtime"]["worker"]["state"] == "UNOBSERVED"
    assert value["runtime"]["state"] == "ATTENTION"
    assert value["budget"]["daily_limit_micro_cny"] == 20_000_000
    assert value["budget"]["used_micro_cny"] == 0
    assert value["budget"]["settled_micro_cny"] == 0


def test_configuration_is_versioned_encrypted_idempotent_and_requires_restart(client, core):
    headers = login(client)
    before = client.get("/v1/console/configuration").json()
    assert before["version"] == before["active_version"] == 0
    secret = "synthetic-model-key-not-for-disclosure"
    body = {"expected_version": 0, "changes": {"max_output_tokens": 1024, "model_key": secret}}
    first = client.put("/v1/console/configuration", json=body, headers=headers)
    assert first.status_code == 200, first.text
    assert secret not in first.text
    assert first.json()["version"] == 1 and first.json()["pending_restart"]
    duplicate = client.put("/v1/console/configuration", json=body, headers=headers)
    assert duplicate.json() == first.json()
    assert core.settings.max_output_tokens == 768
    saved = core.store.one("SELECT * FROM operator_configuration")
    assert secret not in json.dumps(saved)
    assert secret not in json.dumps(core.store.rows("SELECT * FROM audit_log"))
    reloaded = operator_config.load(core.settings)
    assert reloaded.max_output_tokens == 1024 and reloaded._operator_config_version == 1
    assert reloaded.model_key.get_secret_value() == secret
    assert not operator_config.view(core.store, reloaded)["pending_restart"]
    response = client.put("/v1/console/configuration", json={
        "expected_version": 0, "changes": {"max_output_tokens": 2048},
    }, headers={**headers, "Idempotency-Key": "another-configuration"})
    assert response.status_code == 409
    rejected = client.put("/v1/console/configuration", json={
        "expected_version": 1, "changes": {"max_output_tokens": 1000000, "model_key": secret},
    }, headers={**headers, "Idempotency-Key": "invalid-configuration"})
    assert rejected.status_code == 400 and secret not in rejected.text
    forbidden = client.put("/v1/console/configuration", json={
        "expected_version": 1, "changes": {"owner_qq_id": "999999"},
    }, headers={**headers, "Idempotency-Key": "forbidden-identity"})
    assert forbidden.status_code == 400


def test_memory_correction_and_erasure_keep_receipts_without_private_copies(client, core):
    source = ingest(core, text="我喜欢龙井茶")
    with core.store.transaction(control_write=True):
        memory_id = core.memory.add(Candidate(subject="user", kind="preference", fact_key="tea",
            content="主人喜欢龙井茶", evidence_event_id=source["event_id"],
            evidence_quote="我喜欢龙井茶"), core.clock(), confirmed=True)
    headers = login(client)
    listing = client.get("/v1/console/memories")
    assert listing.status_code == 200, listing.text
    assert memory_id in {row["id"] for row in listing.json()["items"]}
    text = "主人更喜欢白茶"
    body = {"decision": "correct", "corrected_content": text}
    result = client.post(f"/v1/console/memories/{memory_id}/decision", json=body, headers=headers)
    assert result.status_code == 200, result.text
    assert client.post(f"/v1/console/memories/{memory_id}/decision", json=body,
                       headers=headers).json() == result.json()
    new_id = result.json()["memory_id"]
    assert new_id != memory_id
    assert text not in json.dumps(core.store.rows("SELECT * FROM console_operations"))
    forgotten = client.post(f"/v1/console/memories/{new_id}/decision", json={"decision": "forget"},
                            headers={**headers, "Idempotency-Key": "forget-corrected-memory"})
    assert forgotten.status_code == 200, forgotten.text
    assert forgotten.json()["state"] == "FORGOTTEN"
    assert new_id not in {row["id"] for row in client.get("/v1/console/memories").json()["items"]}
    assert core.store.one("SELECT state FROM privacy_erasure_ledger")["state"] == "COMPLETE"


def test_cloud_budget_change_uses_its_own_version_and_preserves_native_rows(client, core):
    headers = login(client)
    policy = client.get("/v1/console/configuration").json()["cloud_budget"]
    response = client.put("/v1/console/cloud-budget", json={
        "expected_version": policy["version"], "changes": {"daily_micro_cny": 18_000_000},
    }, headers=headers)
    assert response.status_code == 200, response.text
    overview = client.get("/v1/console/overview").json()
    assert overview["budget"]["daily_limit_micro_cny"] == 18_000_000
    assert client.get("/v1/console/configuration").json()["version"] == 0
    assert not core.store.rows("SELECT * FROM model_usage")


def test_session_expires_and_credential_rotation_revokes_it(client, core):
    login(client)
    core.clock.advance(core.settings.console.session_seconds + 1)
    assert client.get("/v1/console/session").status_code == 401
    login(client)
    core.settings.control_token = SecretStr("rotated-console-password-" * 2)
    assert client.get("/v1/console/session").status_code == 401


def test_login_failures_are_bounded_and_secret_fields_are_never_echoed(client):
    for _ in range(5):
        assert client.post("/v1/console/session", json={"password": "wrong"},
                           headers={"Origin": ORIGIN}).status_code == 401
    assert client.post("/v1/console/session", json={"password": PASSWORD},
                       headers={"Origin": ORIGIN}).status_code == 429


def test_nested_write_only_keys_survive_roundtrip_and_partial_desired_updates(client, core):
    active, desired = "synthetic-active-vision-key", "synthetic-desired-vision-key"
    core.settings.content.vision.key = SecretStr(active)
    headers = login(client)

    def save(version, changes):
        response = client.put("/v1/console/configuration", json={
            "expected_version": version, "changes": changes,
        }, headers={**headers, "Idempotency-Key": f"secret-roundtrip-{version}"})
        assert response.status_code == 200, response.text
        assert active not in response.text and desired not in response.text
        return response.json()

    first = save(0, {"content": {"vision": {"key": desired, "model": "new-vision"}}})
    content = next(field for field in first["fields"] if field["key"] == "content")
    assert content["value"]["vision"]["key"] is None
    assert content["active_value"]["vision"]["key"] is None
    echoed = content["value"]
    echoed["max_image_bytes"] = 2048
    save(1, {"content": echoed, "video_model_key": None})
    save(2, {"content": {"max_context_chars": 10000}, "model_key": None})
    restarted = operator_config.load(core.settings)
    assert restarted.content.vision.key.get_secret_value() == desired
    assert restarted.content.vision.model == "new-vision"
    assert restarted.content.max_image_bytes == 2048
    assert restarted.content.max_context_chars == 10000
    assert restarted.video_model_key.get_secret_value() == ""
    assert restarted._operator_config_version == 3
    assert core.settings.content.vision.key.get_secret_value() == active
    save(3, {"content": {"vision": {"key": ""}}})
    save(4, {"content": {"vision": {"key": None}, "wait_seconds": 35}})
    assert operator_config.load(core.settings).content.vision.key.get_secret_value() == ""
    for table in ("operator_configuration", "audit_log", "console_operations"):
        stored = encode(core.store.rows("SELECT * FROM " + table))
        assert active not in stored and desired not in stored


@pytest.mark.parametrize("invalid", ["url", "key_type", "field"])
def test_nested_configuration_errors_do_not_echo_or_persist_secret_input(client, core, invalid):
    headers = login(client)
    secret = "synthetic-secret-rejected-nested-value"
    vision = {"key": secret}
    if invalid == "url":
        vision["url"] = "https://example.test/?token=" + secret
    elif invalid == "key_type":
        vision["key"] = {"secret": secret}
    else:
        vision["unsupported_" + secret] = secret
    response = client.put("/v1/console/configuration", json={
        "expected_version": 0, "changes": {"content": {"vision": vision}},
    }, headers=headers)
    assert response.status_code == 400
    assert response.json() == {"error": "INVALID_OPERATOR_CONFIGURATION"}
    assert secret not in response.text
    assert core.store.rows("SELECT * FROM operator_configuration") == []
    assert secret not in encode(core.store.rows("SELECT * FROM audit_log"))


def test_extracted_candidate_appears_in_pending_and_leaves_after_owner_confirmation(client, core):
    from companion.core import Core

    core.settings.extraction_min_events = 1
    core.settings.extraction_idle_seconds = 0
    source = ingest(core, text="我喜欢天文主题")
    assert run(core.run_once())
    core.model.extraction = encode({"memories": [{
        "subject": "user", "kind": "preference", "fact_key": "topic",
        "content": "主人喜欢天文主题", "evidence_event_id": source["event_id"],
        "evidence_quote": "我喜欢天文主题"}]})
    with Core(core.settings, model=core.model, clock=core.clock) as worker:
        assert worker.store is not client.app_core.store and worker.store is not core.store
        assert run(worker.extract_once())
    candidate = core.store.one("SELECT * FROM memories")
    assert candidate["status"] == "HYPOTHESIS" and candidate["needs_review"] == 0
    assert client.get("/v1/console/memories?review=true").status_code == 401
    headers = login(client)
    listed = client.get("/v1/console/memories?review=true").json()
    assert listed["meta"]["pending_count"] == 1
    pending = listed["items"]
    assert [item["id"] for item in pending] == [candidate["memory_id"]]
    assert pending[0]["detail"]["needs_review"] is False
    assert client.get("/v1/console/overview").json()["counts"]["review_memories"] == 1
    assert core.store.one("SELECT * FROM memories") == candidate
    usage = core.store.rows("SELECT * FROM model_usage ORDER BY request_id")
    path = f"/v1/console/memories/{candidate['memory_id']}/decision"
    response = client.post(path, json={"decision": "confirm"}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "CONFIRMED"
    duplicate = client.post(path, json={"decision": "confirm"}, headers=headers)
    assert duplicate.json() == response.json()
    listed = client.get("/v1/console/memories?review=true").json()
    assert listed["items"] == [] and listed["meta"]["pending_count"] == 0
    assert client.get("/v1/console/overview").json()["counts"]["review_memories"] == 0
    assert client.get("/v1/console/memories").json()["items"][0]["status"] == "CONFIRMED"
    assert core.store.one("SELECT * FROM memories")["origin"] == "user_confirmed"
    assert core.store.one("SELECT COUNT(*) n FROM audit_log WHERE action='memory.owner_decision'")[
        "n"] == 1
    assert core.store.rows("SELECT * FROM model_usage ORDER BY request_id") == usage


def test_pending_list_and_count_include_candidates_and_confirmed_conflicts(client, core):
    candidate, _ = stored_memory(core, confirmed=False)
    candidate_conflict, _ = stored_memory(core, 2, confirmed=False, review=True, content="候选冲突")
    confirmed_conflict, _ = stored_memory(core, 3, review=True, content="已确认冲突")
    confirmed, _ = stored_memory(core, 4, content="已确认事实")
    login(client)
    items = client.get("/v1/console/memories?review=true").json()["items"]
    assert {item["id"] for item in items} == {candidate, candidate_conflict, confirmed_conflict}
    assert confirmed not in {item["id"] for item in items}
    assert client.get("/v1/console/overview").json()["counts"]["review_memories"] == 3
    result = client.get("/v1/console/memories?review=true&q=候选冲突").json()["items"]
    assert [item["id"] for item in result] == [candidate_conflict]
    assert result[0]["detail"]["needs_review"] is True
    empty = client.get("/v1/console/memories?review=true&q=不存在").json()
    assert empty["items"] == [] and empty["meta"]["pending_count"] == 3


def test_pending_count_covers_candidates_beyond_the_list_limit_and_sql_page(client, core):
    _, source = stored_memory(core, confirmed=False)
    for number in range(2, 207):
        core.clock.advance(1)
        stored_memory(core, number, source=source, content=f"候选事实 {number}", confirmed=False)
    login(client)
    listed = client.get("/v1/console/memories?review=true&limit=2").json()
    assert listed["meta"]["pending_count"] == 206
    items = listed["items"]
    assert len(items) == 2 and all(item["status"] == "HYPOTHESIS" for item in items)
    assert client.get("/v1/console/overview").json()["counts"]["review_memories"] == 206


def test_memory_projection_pages_native_candidates_and_keeps_literal_unicode_search(
    client, core, monkeypatch,
):
    target, source = stored_memory(core, content="Straße 100%_ready", review=True)
    for number in range(2, 207):
        core.clock.advance(1)
        stored_memory(core, number, source=source, content=f"其他项目记录 {number}")
    from companion import retrieval_work

    # Administrative totals and literal catalog pages remain complete when a reply
    # would have insufficient nomination allowance for this population.
    monkeypatch.setitem(retrieval_work.LIMITS, "candidates", 1)
    login(client)
    result = client.get("/v1/console/memories", params={"q": "STRASSE 100%_READY", "limit": 1})
    assert result.status_code == 200, result.text
    assert [row["id"] for row in result.json()["items"]] == [target]
    assert result.json()["items"][0]["detail"]["evidence_ids"] == [source["event_id"]]
    result = client.get("/v1/console/memories", params={"review": "true"})
    assert [row["id"] for row in result.json()["items"]] == [target]
    result = client.get("/v1/console/memories", params={"q": "不存在的字面片段"})
    assert result.status_code == 200 and result.json()["items"] == []


@pytest.mark.parametrize("invalid", [
    "negative", "tombstone", "expired", "conversation", "upstream", "future", "ended",
])
@pytest.mark.parametrize("confirmed", [False, True])
def test_memory_projection_preserves_native_source_and_validity_filters(
    client, core, invalid, confirmed,
):
    memory_id, source = stored_memory(core, confirmed=confirmed, review=confirmed)
    login(client)
    assert [row["id"] for row in client.get("/v1/console/memories").json()["items"]] == [memory_id]
    pending = client.get("/v1/console/memories?review=true").json()["items"]
    assert [row["id"] for row in pending] == [memory_id]
    assert client.get("/v1/console/overview").json()["counts"]["review_memories"] == 1
    if invalid == "negative":
        core.store.execute("UPDATE memory_evidence SET polarity=-1")
    elif invalid == "tombstone":
        core.store.execute("INSERT INTO runtime_meta VALUES (%s, 'erased')",
                           ("erased_event:" + source["event_id"],))
    elif invalid == "expired":
        core.store.execute('UPDATE events SET ingested_at=%s WHERE event_id=%s',
                           (after(core.clock(), -366 * 86400), source["event_id"]))
    elif invalid == "conversation":
        core.store.execute("UPDATE conversations SET scope='group'")
    elif invalid == "upstream":
        upstream = ingest(core, 2, "原始来源")
        core.store.insert("event_dependencies", event_id=source["event_id"],
                          source_event_id=upstream["event_id"])
        core.store.execute((
            'UPDATE events SET redacted=1 WHERE event_id=%s'
        ), (upstream["event_id"],))
    elif invalid == "future":
        core.store.execute('UPDATE memories SET valid_from=%s', (after(core.clock(), 1),))
    else:
        core.store.execute('UPDATE memories SET valid_to=%s', (core.clock(),))
    result = client.get("/v1/console/memories")
    assert result.status_code == 200, result.text
    assert result.json()["items"] == []
    assert client.get("/v1/console/memories?review=true").json()["items"] == []
    assert client.get("/v1/console/overview").json()["counts"]["review_memories"] == 0


def test_memory_projection_rechecks_erasure_between_candidate_and_final_read(
    client, core, monkeypatch,
):
    from companion import console_read

    memory_id, _ = stored_memory(core)
    original, calls = console_read._memory_page, 0

    def erase_after_read(*args, **kwargs):
        nonlocal calls
        result = list(original(*args, **kwargs))
        calls += 1
        if calls == 1:
            core.memory.decide(memory_id, "forget", core.clock())
        return result

    monkeypatch.setattr(console_read, "_memory_page", erase_after_read)
    login(client)
    result = client.get("/v1/console/memories")
    assert result.status_code == 409 and result.json() == {"error": "PRIVACY_CHANGED"}
    assert "主人喜欢龙井茶" not in result.text


def test_memory_projection_does_not_mask_disabled_access_as_an_empty_collection(client, core):
    login(client)
    assert client.get("/v1/console/memories").json()["items"] == []
    core.settings.memory_enabled = False
    result = client.get("/v1/console/memories")
    assert result.status_code == 403 and result.json() == {"error": "MEMORY_ACCESS_DENIED"}


def source_projection_records(core):
    source = ingest(core, 1, "主人发送的图片")
    core.store.execute((
        "UPDATE events SET body_json=%s,event_type='message.media_received' "
        'WHERE event_id=%s'
    ), (encode({"text": "", "media": []}), source["event_id"]))
    now = core.clock()
    core.store.insert(
        "content_items", item_id="content_console", owner_event_id=source["event_id"],
        kind="qq_image", slot=0, state="SUCCEEDED", captured_privacy_epoch=0,
        deadline=after(now, 60), created_at=now, updated_at=now,
    )
    job = core.new_job(source["event_id"], "meme_caption", "console-caption", now)
    core.store.insert(
        "meme_captions", job_id=job, cache_key="fixture", request_sha256="a" * 64,
        asset_sha256="b" * 64, source_sha256="c" * 64, identity_json="{}", context_json="{}",
        source_json="{}", request_json="{}", plan_sha256="d" * 64, privacy_epoch=0,
        created_at=now, updated_at=now,
    )
    return source, job


@pytest.mark.parametrize("invalid", ["expired", "tombstone", "conversation", "upstream"])
def test_content_and_meme_views_require_current_private_source_custody(client, core, invalid):
    source, _ = source_projection_records(core)
    login(client)
    for view in ("content", "memes"):
        response = client.get("/v1/console/" + view)
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1
    if invalid == "expired":
        core.store.execute('UPDATE events SET ingested_at=%s WHERE event_id=%s',
                           (after(core.clock(), -366 * 86400), source["event_id"]))
    elif invalid == "tombstone":
        core.store.execute("INSERT INTO runtime_meta VALUES (%s, 'erased')",
                           ("erased_event:" + source["event_id"],))
    elif invalid == "conversation":
        core.store.execute("UPDATE conversations SET scope='group'")
    else:
        upstream = ingest(core, 2, "上游原始证据")
        core.store.insert("event_dependencies", event_id=source["event_id"],
                          source_event_id=upstream["event_id"])
        core.store.execute((
            'UPDATE events SET redacted=1 WHERE event_id=%s'
        ), (upstream["event_id"],))
    for view in ("content", "memes"):
        response = client.get("/v1/console/" + view)
        assert response.status_code == 200 and response.json()["items"] == []
    assert core.store.one("SELECT state FROM content_items")["state"] == "SUCCEEDED"
    assert core.store.one("SELECT COUNT(*) n FROM meme_captions")["n"] == 1


def test_content_and_meme_views_check_independent_related_sources(client, core):
    _, job = source_projection_records(core)
    related = ingest(core, 2, "另一个被引用的来源")
    core.store.execute('UPDATE content_items SET source_event_id=%s', (related["event_id"],))
    core.store.insert("job_sources", job_id=job, event_id=related["event_id"])
    login(client)
    assert len(client.get("/v1/console/content").json()["items"]) == 1
    assert len(client.get("/v1/console/memes").json()["items"]) == 1
    core.store.execute('UPDATE events SET redacted=1 WHERE event_id=%s', (related["event_id"],))
    assert client.get("/v1/console/content").json()["items"] == []
    assert client.get("/v1/console/memes").json()["items"] == []


def test_overview_review_count_matches_visible_sources_and_reports_disabled_as_unavailable(
    client, core,
):
    _, source = stored_memory(core, review=True)
    login(client)
    assert client.get("/v1/console/overview").json()["counts"]["review_memories"] == 1
    core.store.execute("INSERT INTO runtime_meta VALUES (%s, 'erased')",
                       ("erased_event:" + source["event_id"],))
    assert client.get("/v1/console/memories", params={"review": "true"}).json()["items"] == []
    assert client.get("/v1/console/overview").json()["counts"]["review_memories"] == 0
    core.settings.memory_enabled = False
    response = client.get("/v1/console/overview")
    assert response.status_code == 200, response.text
    assert response.json()["counts"]["review_memories"] is None


def test_life_projection_rejects_diary_erasure_during_combined_read(client, core, monkeypatch):
    from companion.core import Core
    from tests.support.diary import DAY, generate, seed_day

    seed_day(core)
    assert generate(core)["state"] == "COMMITTED"
    title = core.diary.get_day(DAY, core.settings.owner_qq_id)["title"]
    login(client)
    recent, erased = client.app_core.goals.recent, False

    def erase_between_projections():
        nonlocal erased
        if not erased:
            with Core(core.settings, clock=core.clock) as other:
                other.diary.forget(DAY, core.settings.owner_qq_id)
            erased = True
        return recent()

    monkeypatch.setattr(client.app_core.goals, "recent", erase_between_projections)
    response = client.get("/v1/console/life")
    assert response.status_code == 409 and response.json()["error"] == "PRIVACY_CHANGED"
    assert title not in response.text
    assert client.get("/v1/console/life").json()["items"] == []


@pytest.mark.parametrize("supplied,canonical", [
    ("https://CONSOLE.example.test:443/", ORIGIN),
    ("https://CONSOLE.example.test:8443", "https://console.example.test:8443"),
    ("https://[::1]:443/", "https://[::1]"),
    ("https://例子.测试", "https://xn--fsqu00a.xn--0zwm56d"),
])
def test_console_origin_matches_browser_serialization(supplied, canonical):
    assert operator_config.ConsoleConfig(public_origin=supplied).public_origin == canonical


@pytest.mark.parametrize("supplied", [
    "https://console.example.test:bad", "https://console.example.test:65536",
    "https://@console.example.test", "https://console.example.test/path",
    "https://console.example.test?x=1", "https://console.example.test#fragment",
])
def test_console_origin_rejects_invalid_ports_and_non_origin_parts(supplied):
    with pytest.raises(ValidationError):
        operator_config.ConsoleConfig(public_origin=supplied)


def test_nullable_object_configuration_roundtrips_enabled_and_null(client, core):
    headers = login(client)
    before = client.get("/v1/console/configuration").json()
    field = next(item for item in before["fields"] if item["key"] == "segmented_reply")
    assert field["value"] is None and field["type"] == "object" and field["nullable"]
    body = {"expected_version": 0, "changes": {"segmented_reply": {"interval_ms": 1000}}}
    enabled = client.put("/v1/console/configuration", json=body, headers=headers)
    assert enabled.status_code == 200, enabled.text
    field = next(item for item in enabled.json()["fields"] if item["key"] == "segmented_reply")
    assert field["value"]["interval_ms"] == 1000 and field["nullable"]
    assert operator_config.load(core.settings).segmented_reply.interval_ms == 1000
    disabled = client.put("/v1/console/configuration", json={
        "expected_version": 1, "changes": {"segmented_reply": None},
    }, headers={**headers, "Idempotency-Key": "restore-default-segmented-reply"})
    assert disabled.status_code == 200, disabled.text
    field = next(item for item in disabled.json()["fields"] if item["key"] == "segmented_reply")
    assert field["value"] is None and field["type"] == "object" and field["nullable"]
    assert operator_config.load(core.settings).segmented_reply is None


def test_console_budget_reports_restore_reconciliation_without_new_spendable_pool(client, core):
    login(client)
    core.store.execute("INSERT INTO runtime_meta VALUES ('cloud_budget_restore_required','1')")
    response = client.get("/v1/console/overview")
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["runtime"]["state"] == "ATTENTION"
    assert value["budget"]["restore_reconciliation_required"]
    assert value["budget"]["restored_history_may_be_incomplete"]
    assert value["budget"]["remaining_micro_cny"] == 0
    assert value["budget"]["admission_blocked"]

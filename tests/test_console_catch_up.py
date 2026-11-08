"""Console views for style candidates, the watch ledger and every configuration field."""

import json

from pydantic import SecretStr

from companion import operator_config
from companion.config import Settings
from tests.support.console_cognition import PASSWORD, client, login  # noqa: F401
from tests.support.style_distill import PATTERN, candidates, comment, thread
from tests.support.style_distill import ready as style_ready
from tests.support.style_distill import step as style_step
from tests.support.watch_ledger import JUDGEMENT, observe
from tests.support.watch_ledger import ready as watch_ready
from tests.support.watch_ledger import step as watch_step


def candidate(core, tmp_path, pattern=PATTERN, number=3):
    style_ready(core, tmp_path, json.dumps(pattern, ensure_ascii=False))
    thread(core, number, [comment("1", "这只猫睡觉的姿势也太离谱了"),
                          comment("2", "像是被谁按了暂停键一样", parent="1")])
    style_step(core)
    core.settings.control_token = SecretStr(PASSWORD)  # The owner helper sets its own.
    return candidates(core)[0]["pattern_sha256"]


def test_every_configuration_field_has_a_chinese_label_or_is_custody():
    fields = set(Settings.model_fields)
    assert fields <= set(operator_config.LABELS) | operator_config.EXCLUDED
    assert set(operator_config.GROUPS) <= fields


def test_conversation_policy_is_always_an_editable_value():
    settings = Settings(owner_qq_id="10001", bot_qq_id="10002", bridge_token="x" * 32,
                        database="dbname=synthetic",
                        conversation_policy=None)  # An earlier operator version stored null.
    assert (settings.conversation_policy.mode, settings.conversation_policy.strategy) == (
        "ENABLED", "participation_v2")
    assert "conversation_policy" not in settings.model_dump()
    off = settings.model_copy(update={"conversation_policy": settings.conversation_policy
                                      .model_copy(update={"mode": "OFF"})})
    assert Settings.model_validate(off.model_dump()).conversation_policy.mode == "OFF"


def test_configuration_view_shows_the_effective_policy(client, core):  # noqa: F811
    login(client)
    fields = {f["key"]: f for f in client.get("/v1/console/configuration").json()["fields"]}
    policy = fields["conversation_policy"]
    assert policy["label"] == "对话策略" and policy["group"] == "expression"
    # The fixture pins the pre-default baseline; the view shows whatever is effective.
    assert policy["value"] == core.settings.conversation_policy.model_dump(mode="json")
    assert set(policy["value"]) == {"mode", "strategy"}
    assert fields["chat_thinking"]["group"] == "model"


def test_style_candidates_are_listed_without_source_text_and_approved_once(
        client, core, tmp_path):  # noqa: F811
    key = candidate(core, tmp_path)
    headers = login(client)
    page = client.get("/v1/console/style-candidates").json()
    [item] = page["items"]
    assert (item["state"], item["approvable"], item["short_id"]) == ("CANDIDATE", True, key[:8])
    assert item["response_action"] == PATTERN["response_action"]
    assert "暂停键" not in json.dumps(page, ensure_ascii=False)
    url = f"/v1/console/style-candidates/{key}/decision"
    keyed = {**headers, "Idempotency-Key": "style-decision-1"}
    first = client.post(url, json={"decision": "APPROVE"}, headers=keyed)
    assert first.status_code == 200, first.text
    assert client.post(url, json={"decision": "APPROVE"}, headers=keyed).json() == first.json()
    [row] = candidates(core)
    assert (row["state"], row["approved_version"]) == ("APPROVED", 1)
    assert client.get("/v1/console/style-candidates").json()["items"][0]["state"] == "APPROVED"
    again = client.post(url, json={"decision": "REJECT"},
                        headers={**headers, "Idempotency-Key": "style-decision-2"})
    assert again.status_code == 404
    assert client.post("/v1/console/style-candidates/short/decision",
                       json={"decision": "REJECT"},
                       headers={**headers, "Idempotency-Key": "style-decision-3"}
                       ).status_code == 404


def test_risky_style_candidate_can_only_be_rejected_from_the_console(
        client, core, tmp_path):  # noqa: F811
    key = candidate(core, tmp_path, {**PATTERN, "risk_flags": ["circle_meme"]}, number=4)
    headers = login(client)
    assert client.get("/v1/console/style-candidates").json()["items"][0]["approvable"] is False
    url = f"/v1/console/style-candidates/{key}/decision"
    refused = client.post(url, json={"decision": "APPROVE"},
                          headers={**headers, "Idempotency-Key": "style-risk-1"})
    assert refused.status_code == 409
    rejected = client.post(url, json={"decision": "REJECT", "reason": "圈子梗"},
                           headers={**headers, "Idempotency-Key": "style-risk-2"})
    assert rejected.status_code == 200, rejected.text
    assert candidates(core)[0]["state"] == "REJECTED"


def test_watch_ledger_lists_judgements_without_source_ids(client, core, tmp_path):  # noqa: F811
    watch_ready(core, tmp_path, json.dumps(JUDGEMENT, ensure_ascii=False))
    observe(core, 1)
    assert watch_step(core)
    core.settings.control_token = SecretStr(PASSWORD)
    login(client)
    page = client.get("/v1/console/watch-ledger").json()
    [item] = page["items"]
    assert (item["title"], item["take"], item["depth"]) == ("猫和纸箱", JUDGEMENT["take"], "text")
    assert "source_event_id" not in item and "watch_obs_1" not in json.dumps(page)


def test_new_reads_require_the_owner_session(client):  # noqa: F811
    for path in ("/style-candidates", "/watch-ledger"):
        assert client.get("/v1/console" + path).status_code == 401

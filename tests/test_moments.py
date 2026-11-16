"""Owner-only moments keep Life's exact wording, its fences and native erasure."""

import json

import pytest
from pydantic import ValidationError

from companion import moments
from companion.life_protocol import LifeShadowApproval
from companion.store import after
from tests.support.life_expression_consumption import (
    close_provider_clients as close_provider_clients,
)
from tests.support.life_expression_consumption import prepared
from tests.support.owner_console import client as client
from tests.support.owner_console import login

TEXT = "这段关于恒星颜色的介绍让我想再弄懂光谱与温度的关系。"


def moment_ready(core, tmp_path, **options):
    return prepared(core, tmp_path, destination="PRIVATE_MOMENT", purposes=["moments"],
                    life_purposes=["PRIVATE_MOMENT"], automatic_actions=["moment.save"],
                    **options)


def test_automatic_moment_keeps_the_settled_wording_once_without_a_model_call(core, tmp_path):
    job, _, _, source = moment_ready(core, tmp_path)
    usage, calls = core.store.rows("SELECT * FROM model_usage"), list(core.model.calls)
    assert core.life_topics.consume_due(diary=core.diary)
    assert not core.life_topics.consume_due(diary=core.diary)
    [row] = core.store.rows("SELECT * FROM moments")
    assert (row["text"], row["state"], row["trigger"], row["source_job_id"]) == (
        TEXT, "PUBLISHED", "LIFE", job)
    assert source in json.loads(row["source_event_ids_json"])
    assert core.store.rows("SELECT * FROM model_usage") == usage and core.model.calls == calls
    assert core.store.rows("SELECT * FROM outbox") == []
    assert [item["text"] for item in moments.items(core.store)] == [TEXT]


@pytest.mark.parametrize("fence", ["quiet", "limit"])
def test_quiet_hours_and_daily_limit_hold_the_moment_back(core, tmp_path, fence):
    moment_ready(core, tmp_path)
    if fence == "quiet":
        core.settings.quiet_start, core.settings.quiet_end = 0, 23
    else:
        core.settings.moments_daily_limit = 0
    assert not core.life_topics.consume_due(diary=core.diary)
    assert core.store.rows("SELECT * FROM moments") == []


def test_hide_forget_and_source_erasure(core, tmp_path):
    _, _, _, source = moment_ready(core, tmp_path)
    core.life_topics.consume_due(diary=core.diary)
    moment_id = core.store.one("SELECT moment_id FROM moments")["moment_id"]
    assert moments.set_hidden(core.store, moment_id, True, core.clock())["state"] == "HIDDEN"
    assert moments.items(core.store) == []
    assert len(moments.items(core.store, include_hidden=True)) == 1
    moments.set_hidden(core.store, moment_id, False, core.clock())
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, [source], core.clock())
    row = core.store.one("SELECT * FROM moments")
    assert (row["state"], row["text"]) == ("ERASED", "")
    with pytest.raises(Exception, match="MOMENT_NOT_FOUND"):
        moments.forget(core.store, core.ledger, moment_id, core.clock())


def test_owner_forget_goes_through_the_ledger(core, tmp_path):
    moment_ready(core, tmp_path)
    core.life_topics.consume_due(diary=core.diary)
    moment_id = core.store.one("SELECT moment_id FROM moments")["moment_id"]
    with core.store.transaction(control_write=True):
        assert moments.forget(core.store, core.ledger, moment_id, core.clock())["state"] == "ERASED"
    assert core.store.one("SELECT state, text FROM moments") == {"state": "ERASED", "text": ""}
    record = [r for r in core.ledger.read()[1:] if r.get("target_type") == "moment"]
    assert [r["memory_id"] for r in record] == [moment_id]


def test_life_scope_rejects_an_automatic_moment_without_its_destination(core):
    with pytest.raises(ValidationError):
        LifeShadowApproval(scope="owner_private_life_shadow_v1", authorization_sha256="c" * 64,
                           conversation_id="conv", source_event_ids=["evt_source"],
                           purposes=["PRIVATE_DIARY"],
                           expires_at=after(core.clock(), 86400), max_items=2,
                           automatic_actions=["moment.save"])


def test_console_lists_hides_and_forgets_moments(client, core, tmp_path):
    moment_ready(core, tmp_path)
    core.life_topics.consume_due(diary=core.diary)
    headers = login(client)
    [item] = client.get("/v1/console/moments").json()["items"]
    assert (item["text"], item["state"], item["trigger"]) == (TEXT, "PUBLISHED", "LIFE")
    path = f"/v1/console/moments/{item['id']}"
    hidden = client.post(path + "/visibility", json={"hidden": True}, headers=headers)
    assert hidden.status_code == 200, hidden.text
    assert client.get("/v1/console/moments").json()["items"][0]["state"] == "HIDDEN"
    forgot = client.post(path + "/forget", json={},
                         headers={**headers, "Idempotency-Key": "forget-moment"})
    assert forgot.status_code == 200 and forgot.json()["state"] == "ERASED"
    assert client.get("/v1/console/moments").json()["items"] == []

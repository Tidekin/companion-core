import json

import httpx

from companion import style_distill
from companion.model import ModelClient
from tests.conftest import run
from tests.support.exploration import configure
from tests.support.owner_authority import activate
from tests.support.owner_authority_setup import prepare
from tests.support.watch_ledger import Provider, observe

PATTERN = {
    "context_tags": ["daily_share", "light_humor"],
    "attention_target": "前文里一个具体的反差细节",
    "response_action": "抓住这个细节做一次轻度夸张的联想",
    "appropriate_when": "轻松分享，对方没有求助或表达痛苦",
    "avoid_when": "认真求助、愤怒指责、痛苦经历或关系不明",
    "risk_flags": [],
}


def comment(cid, text, parent=None):
    return {"comment_id": cid, "reply_to_id": parent, "floor": None, "author": "SYNTHETIC作者",
            "text": text, "image_count": 0, "truncated": False}


def thread(core, number, items):
    event_id = observe(core, number)
    evidence = {"items": items, "reported_total": 100, "partial": True}
    core.store.execute(
        (
            'UPDATE observations SET '
            "evidence_json=jsonb_set(evidence_json::jsonb,'{reading}',%s::jsonb)::text WHERE"
            ' event_id=%s'
        ), (json.dumps({"comments": evidence}, ensure_ascii=False), event_id))
    return event_id


def ready(core, tmp_path, *texts):
    configure(core)
    core.settings.style_distill = core.settings.style_distill.model_copy(update={"enabled": True})
    policy = prepare(core, tmp_path)
    activate(core, policy.model_copy(update={"purposes": ["exploration", "expression"]}))
    provider = Provider(*texts)
    core.model = ModelClient(core.settings,
                             httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    return provider


def step(core):
    return run(style_distill.run_once(core.store, core.settings, core.model, core.clock))


def candidates(core):
    return core.store.rows("SELECT * FROM style_candidates")

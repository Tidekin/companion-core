"""Core-owned source freezing; existing native authority and settled caption evidence."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from companion import meme_policy, meme_service, owner_authority
from companion.config import Settings
from companion.contracts import CoreError
from companion.expression import ExpressionEngine
from companion.meme_retrieval import blocked
from companion.owner_authority_protocol import AuthorityStop
from companion.store import after, encode
from companion_meme_protocol import CaptionResult, parse
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.meme_semantic import caption_eligible as bridge_eligible
from companion_onebot_bridge.meme_semantic_search import SemanticQuery
from companion_onebot_bridge.meme_semantic_search import context_allows as bridge_allows
from companion_onebot_bridge.window import config_matches
from companion_onebot_bridge.window import digest as bridge_digest
from tests.conftest import run
from tests.support.meme_core import CAPTION
from tests.support.meme_core_runtime import RULE
from tests.support.meme_core_runtime import runtime_case as runtime_case


def test_native_evidence_freezes_exact_source_and_registers_prompt_roots(runtime_case):
    c = runtime_case
    before = c.core.store.rows("SELECT * FROM model_usage")
    run(c.service.prepare_offer(c.job))
    frozen = c.retrieval.check(c.job)
    candidate = frozen["offer"]["candidates"][0]
    assert candidate["description"].startswith(CAPTION["description"])
    assert "untrusted catalog" not in candidate["description"]
    binding = frozen["catalog_bindings"][candidate["candidate_id"]]
    assert binding["asset_id"] == c.asset_id and binding["caption_job_id"] == c.caption_id
    assert c.core.store.one(
        'SELECT 1 FROM job_sources WHERE job_id=%s AND event_id=%s',
        (c.job["job_id"], c.source["event_id"]),
    )
    run(c.service.prepare_offer(c.job))
    assert c.retrieval.check(c.job) == frozen
    assert c.core.store.rows("SELECT * FROM model_usage") == before


@pytest.mark.parametrize("change", ["remove", "caption", "usage", "epoch", "binding", "stop"])
def test_frozen_source_cannot_be_replaced_or_silently_dropped(runtime_case, change):
    c = runtime_case
    run(c.retrieval.prepare(c.job))
    s = c.core.store
    if change == "remove":
        s.execute("UPDATE meme_assets SET state='REMOVED',data=NULL,mime=NULL,metadata_json=NULL")
    elif change == "caption":
        s.execute("UPDATE meme_captions SET result_json=NULL WHERE job_id=%s", (c.caption_id,))
    elif change == "usage":
        s.execute("UPDATE model_usage SET state='UNKNOWN'")
    elif change == "epoch":
        from companion.privacy import advance_epoch

        with s.transaction(control_write=True):
            advance_epoch(s)
    elif change == "binding":
        saved = c.retrieval.check(c.job)
        next(iter(saved["catalog_bindings"].values()))["source_sha256"] = "f" * 64
        s.execute(
            "UPDATE runtime_meta SET value=%s WHERE key=%s",
            (encode(saved), "meme_offer:" + c.job["event_id"]),
        )
    else:
        owner_authority.stop(
            s,
            c.core.settings,
            c.core.clock,
            AuthorityStop(operation_id="stop-core-catalog", authority=c.grant.authority),
        )
    with pytest.raises(CoreError):
        c.retrieval.check(c.job)
    with pytest.raises(CoreError):
        ExpressionEngine(s, c.core.settings, c.core.clock).instruction(c.job["event_id"])


def test_catalog_mode_refuses_new_bridge_work_and_does_not_wait(runtime_case):
    c = runtime_case
    assert not c.service.waiting_offer(c.job, c.core.clock())
    with pytest.raises(CoreError, match="MEME_CORE_CATALOG_OWNED"):
        c.service._bridge_catalog()
    with pytest.raises(CoreError, match="MEME_DISCOVERY_DISABLED"):
        meme_service._scope(c.core.store, c.core.settings, c.core.clock)


def test_server_classified_controls_never_require_a_model_offer(runtime_case):
    c = runtime_case
    row = c.core.store.one('SELECT body_json FROM events WHERE event_id=%s', (c.job["event_id"],))
    value = json.loads(row["body_json"])
    c.core.store.execute(
        'UPDATE events SET body_json=%s WHERE event_id=%s',
        (encode({**value, "control_command": True}), c.job["event_id"]),
    )
    run(c.service.prepare_offer(c.job))
    c.service.check_offer(c.job)
    assert not c.core.store.one(
        'SELECT 1 FROM runtime_meta WHERE key=%s', ("meme_offer:" + c.job["event_id"],)
    )
    assert ExpressionEngine(c.core.store, c.core.settings, c.core.clock)._offer(
        c.job["event_id"]
    ) == (True, None)


def test_physical_sha_cooldown_uses_original_sent_time_and_active_holds(runtime_case):
    c = runtime_case
    sha = c.item["asset_sha256"]
    s, now = c.core.store, c.core.clock()
    from companion.transport import enqueue

    message = enqueue(s, c.current["event_id"], c.job["conversation_id"], "sent", now)
    s.execute(
        "UPDATE outbox SET content_json=%s,state='SENT',sent_at=%s WHERE message_id=%s",
        (encode({"text": "sent", "image": {"candidate_id": sha}}), after(now, -1801), message),
    )
    assert not blocked(s, c.core.settings, sha, now)
    s.execute("UPDATE outbox SET sent_at=%s WHERE message_id=%s", (now, message))
    assert blocked(s, c.core.settings, sha, now)
    s.execute(
        "UPDATE outbox SET state='DELIVERY_UNCERTAIN',sent_at=NULL WHERE message_id=%s", (message,)
    )
    assert blocked(s, c.core.settings, sha, after(now, 86401))


@pytest.mark.parametrize("runtime_case", ["two_sources"], indirect=True)
def test_same_bytes_choose_one_exact_origin_and_never_substitute(runtime_case):
    c = runtime_case
    assets = c.core.store.rows("SELECT * FROM meme_assets ORDER BY asset_id")
    assert len(assets) == 2 and assets[0]["source_sha256"] != assets[1]["source_sha256"]
    run(c.retrieval.prepare(c.job))
    saved = c.retrieval.check(c.job)
    assert len(saved["offer"]["candidates"]) == len(saved["catalog_bindings"]) == 1
    selected = next(iter(saved["catalog_bindings"].values()))
    assert selected["asset_id"] == assets[0]["asset_id"]
    c.core.store.execute(
        (
            'UPDATE meme_assets SET '
            "state='REMOVED',data=NULL,mime=NULL,metadata_json=NULL WHERE "
            'asset_id=%s'
        ),
        (selected["asset_id"],),
    )
    assert c.core.store.one("SELECT COUNT(*) AS n FROM meme_assets WHERE state='STAGED'")["n"] == 1
    with pytest.raises(CoreError, match="MEME_ASSET_UNAVAILABLE"):
        c.retrieval.check(c.job)
    with pytest.raises(CoreError):
        run(c.retrieval.prepare(c.job))


def test_erasure_of_original_root_invalidates_derived_offer_without_refunding(runtime_case):
    c = runtime_case
    run(c.retrieval.prepare(c.job))
    before = c.core.store.rows("SELECT * FROM model_usage")
    with c.core.store.transaction(control_write=True):
        c.core.ledger.expire_events(c.core.store, [c.source["event_id"]], c.core.clock())
    assert not c.core.store.one(
        'SELECT 1 FROM runtime_meta WHERE key=%s', ("meme_offer:" + c.job["event_id"],)
    )
    asset = c.core.store.one("SELECT * FROM meme_assets")
    assert asset["state"] == "ERASED" and asset["data"] is asset["metadata_json"] is None
    assert c.core.store.rows("SELECT * FROM model_usage") == before
    with pytest.raises(CoreError):
        c.retrieval.check(c.job)


def test_independent_connections_to_same_authoritative_store_reserve_one_image(runtime_case):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import closing
    from threading import Barrier

    from companion.store import Store

    c = runtime_case
    run(c.retrieval.prepare(c.job))
    sha = c.item["asset_sha256"]
    barrier = Barrier(2)

    def compete(index):
        with closing(Store(c.core.store.database, c.core.store.state_directory)) as store:
            engine = ExpressionEngine(store, c.core.settings, c.core.clock)
            barrier.wait(timeout=10)
            message = engine.enqueue(
                c.job["event_id"],
                c.job["conversation_id"],
                "抱抱你。⬡" + sha + "⬡",
                c.core.clock(),
                key="race:" + str(index),
            )
            return json.loads(
                store.one("SELECT content_json FROM outbox WHERE message_id=%s", (message,))[
                    "content_json"
                ]
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(compete, range(2)))
    assert sum("image" in result for result in results) == 1
    assert len(c.core.store.rows("SELECT * FROM outbox")) == 2


@pytest.mark.parametrize(
    "updates",
    [
        {},
        {"confidence": 0.7},
        {"temporal_status": "uncertain"},
        {"tone": "mocking"},
        {"target": "self"},
        {"visible_text": "你真是笨蛋"},
    ],
)
def test_exact_lexical_gates_match_original_bridge(updates):
    from companion_meme_protocol import sample_frames, validate_bytes
    from tests.support.next_memes_animation import canvas_gif
    from tests.support.next_memes_catalog import png

    data = canvas_gif([(24, 24), (24, 24)]) if updates.get("temporal_status") else png()
    sampling, _ = sample_frames(data, SimpleNamespace(validate_bytes=validate_bytes))
    result = parse(
        CaptionResult, {**CAPTION, **updates, "sampled_frames": sampling.model_dump(mode="json")}
    )
    query = SemanticQuery(text="希望有温暖的抱抱", intent="comfort", target="recipient")
    assert meme_policy.caption_eligible(result) == bridge_eligible(result, 0.8)
    assert meme_policy.context_allows(result, query.model_dump()) == bridge_allows(result, query)


def test_snapshot_binds_scheduled_import_and_preserves_history_fallback():
    # Frozen synthetic snapshots include the scheduled-import setting. A changed
    # scan policy must require new approval rather than match the older digest.
    config = BridgeConfig(
        owner_qq_id="10001",
        bot_qq_id="20002",
        bridge_token="b" * 32,
        onebot_token="s" * 32,
        protocol_epoch="legacy-epoch-01",
    )
    original = "9c7ad9a950df994521dad682ff9cdd7ccbc8bd49e74c0aefb7a4e92debfdbc7e"
    historical = "7e43aa2cf292847b7926398139c69986bd29f4be85bb5a7ec3a9290d5ec98239"
    assert bridge_digest(config.snapshot()) == original
    assert config_matches(config, original) and config_matches(config, historical)
    assert config.snapshot()["scheduled_meme_import_enabled"] is True
    disabled = replace(config, scheduled_meme_import_enabled=False)
    assert not config_matches(disabled, original) and not config_matches(disabled, historical)
    assert not config_matches(
        config, "14d28e7c1564a057994f9bbf494074d420612c18430e680a5f7532564f3e580b",
    )
    assert "history_backfill" not in config.snapshot()
    assert replace(config, history_backfill=True).snapshot()["history_backfill"] is True
    core = replace(config, meme_catalog_owner="core")
    assert core.snapshot()["meme_catalog_owner"] == "core"
    assert not config_matches(core, original) and not config_matches(core, historical)


def test_core_policy_binds_nondefault_rules_even_if_bridge_selected():
    settings = Settings(
        owner_qq_id="10001", bot_qq_id="20002", bridge_token="b" * 32, expression_enabled=True
    )
    original = meme_service.policy_hash(settings)
    assert original == "bc622473bb58029fc341e02be873ee24740eaf38f9721c8c5c461b693d04c43a"
    explicit_defaults = settings.model_copy(
        update={
            "meme_catalog_owner": "bridge",
            "meme_trusted_sources": [],
            "meme_cooldown_seconds": 1800,
        }
    )
    assert meme_service.policy_hash(explicit_defaults) == original
    for changes in (
        {"meme_catalog_owner": "core"},
        {"meme_cooldown_seconds": 1},
        {"meme_trusted_sources": [meme_policy.TrustedSource(**RULE)]},
    ):
        assert meme_service.policy_hash(settings.model_copy(update=changes)) != original


def test_current_exact_license_rule_keeps_original_no_redirect_archive_boundary(runtime_case):
    c = runtime_case
    metadata = dict(c.item["metadata"])
    rules = meme_policy.source_rules(c.core.settings)
    assert meme_policy.trusted_source(metadata, rules)
    acquired = json.loads(metadata["acquisition_json"])
    original_url = acquired["fetch"]["requested_url"]
    redirected = "https://public.example/another-archive.zip"
    acquired["fetch"].update(
        final_url=redirected,
        hops=[
            {"url": original_url, "address": "8.8.8.8", "status": 302},
            {"url": redirected, "address": "8.8.8.8", "status": 200},
        ],
    )
    metadata["acquisition_json"] = encode(acquired)
    assert not meme_policy.trusted_source(metadata, rules)


@pytest.mark.parametrize(
    "field", ["epoch_type", "binding_version_type", "query", "intent", "description", "tags"]
)
def test_frozen_prompt_and_binding_reject_tampering_before_new_model_work(runtime_case, field):
    c = runtime_case
    run(c.retrieval.prepare(c.job))
    saved = c.retrieval.check(c.job)
    if field == "epoch_type":
        saved["privacy_epoch"] = bool(saved["privacy_epoch"])
    elif field == "binding_version_type":
        next(iter(saved["catalog_bindings"].values()))["version"] = True
    elif field == "query":
        saved["query_sha256"] = "f" * 64
    elif field == "intent":
        saved["offer"]["intent"] = "celebrate"
    else:
        saved["offer"]["candidates"][0][field] = (
            "invented" if field == "description" else ["invented"]
        )
    before = c.core.store.rows("SELECT * FROM model_usage")
    c.core.store.execute(
        'UPDATE runtime_meta SET value=%s WHERE key=%s',
        (encode(saved), "meme_offer:" + c.job["event_id"]),
    )
    with pytest.raises(CoreError):
        c.service.check_offer(c.job)
    assert c.core.store.rows("SELECT * FROM model_usage") == before
    assert not c.core.store.rows("SELECT * FROM outbox")

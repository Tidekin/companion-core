"""Core-owned source freezing; existing native authority and settled caption evidence."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from companion import meme_asset_migration, meme_policy, meme_service, owner_authority
from companion.config import Settings
from companion.contracts import CoreError
from companion.core import Core
from companion.expression import ExpressionEngine
from companion.meme_core import CaptionBody, MemeCaptionService
from companion.meme_retrieval import MemeRetrieval, blocked
from companion.owner_authority_protocol import AuthorityStop
from companion.store import after, encode
from companion_meme_protocol import CaptionResult, digest, parse
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.meme_semantic import caption_eligible as bridge_eligible
from companion_onebot_bridge.meme_semantic_search import SemanticQuery
from companion_onebot_bridge.meme_semantic_search import context_allows as bridge_allows
from companion_onebot_bridge.window import config_matches
from companion_onebot_bridge.window import digest as bridge_digest
from tests.conftest import run
from tests.meme_authority_fixture import admit
from tests.support.owner_authority import activate
from tests.test_meme_core import CAPTION, body, caption_environment, worker

RULE = dict(
    repo="synthetic/pack",
    revision="a" * 40,
    license="CC0",
    license_path="LICENSE",
    license_sha256="b" * 64,
    asset_prefix="memes",
    usage_basis="Synthetic public domain",
)


def licensed_payload(core, event, attempt, now):
    payload = body(core, event, attempt, now)
    proof = payload["source_evidence"]
    page = f"https://github.com/{RULE['repo']}/tree/{RULE['revision']}"
    archive = f"https://codeload.github.com/{RULE['repo']}/zip/{RULE['revision']}"
    proof.update(
        source_page=page,
        asset_url=archive,
        revision=RULE["revision"],
        usage_basis=meme_policy.TrustedSource(**RULE).basis,
    )
    acquired = proof["acquisition"]
    acquired.update(
        source_page=page,
        revision=RULE["revision"],
        discovered_asset_url=f"https://raw.githubusercontent.com/{RULE['repo']}/{RULE['revision']}"
        "/memes/like/hug.png",
        archive_member="pack-" + RULE["revision"] + "/memes/like/hug.png",
    )
    acquired["fetch"].update(
        requested_url=archive,
        final_url=archive,
        body_sha256="c" * 64,
        hops=[{"url": archive, "address": "8.8.8.8", "status": 200}],
    )
    payload["asset"]["source_evidence_sha256"] = digest(proof)
    return payload


@pytest.fixture
def runtime_case(core, tmp_path, monkeypatch, request):
    async def setup():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, _, client, config, grant, now, attempt = values
            payload = licensed_payload(core, event, attempt, now)
            old = MemeCaptionService(core.store, core.settings, None, core.clock)
            caption = old.submit(CaptionBody.model_validate(payload))
            calls = []
            assert await worker(core, calls) and len(calls) == 1
            proof = payload["source_evidence"]
            meta = {
                "category": "like",
                "description": "untrusted catalog text",
                "tags": ["x"],
                "suitable": ["comfort"],
                "avoid": [],
                **{
                    k: proof[k]
                    for k in (
                        "source_page",
                        "asset_url",
                        "license",
                        "usage_basis",
                        "fetched_at",
                        "revision",
                    )
                },
                "acquisition_json": encode(proof["acquisition"]),
            }
            custody = {
                "identity": [
                    config.owner_qq_id,
                    config.bot_qq_id,
                    config.origin,
                    config.protocol_epoch,
                ],
                "capture_public_key": config.capture_public_key
                or grant.key.public_key().public_bytes_raw().hex(),
                "authority": grant.authority,
            }
            item = {
                "asset_sha256": payload["asset"]["asset_sha256"],
                "source_sha256": payload["asset"]["source_evidence_sha256"],
                "active": True,
                "metadata": meta,
                "mime": "image/png",
            }
            data = old.store.one(
                "SELECT asset_data FROM meme_captions WHERE job_id=?", (caption["job_id"],)
            )["asset_data"]
            with core.store.transaction():
                imported = meme_asset_migration._apply(
                    core.store, core.settings, {"custody": custody}, [(item, data)]
                )
            assert imported[0]["state"] == "STAGED"
            if getattr(request, "param", None) == "two_sources":
                other = json.loads(json.dumps(payload))
                acquired = other["source_evidence"]["acquisition"]
                acquired["discovered_asset_url"] = acquired["discovered_asset_url"].replace(
                    "hug.png", "another-hug.png"
                )
                acquired["archive_member"] = acquired["archive_member"].replace(
                    "hug.png", "another-hug.png"
                )
                other["asset"]["source_evidence_sha256"] = digest(other["source_evidence"])
                second = old.submit(CaptionBody.model_validate(other))
                assert second["state"] == "SUCCEEDED" and second["cache_hit"]
                second_item = {
                    **item,
                    "source_sha256": other["asset"]["source_evidence_sha256"],
                    "metadata": {**meta, "acquisition_json": encode(acquired)},
                }
                with core.store.transaction():
                    assert (
                        meme_asset_migration._apply(
                            core.store, core.settings, {"custody": custody}, [(second_item, data)]
                        )[0]["state"]
                        == "STAGED"
                    )
            owner_authority.stop(
                core.store,
                core.settings,
                core.clock,
                AuthorityStop(operation_id="stop-bridge-catalog", authority=grant.authority),
            )
            core.settings = core.settings.model_copy(
                update={
                    "meme_catalog_owner": "core",
                    "meme_trusted_sources": [meme_policy.TrustedSource(**RULE)],
                    "meme_cooldown_seconds": 1800,
                    "meme_discovery_enabled": False,
                }
            )
            variant = getattr(request, "param", None)
            if variant == "semantics_off":
                core.settings.meme_semantics_enabled = False
            if variant == "expression_off":
                core.settings.expression_enabled = False
            # The older discovery fixture uses model_copy(probability=1), which
            # bypasses Settings' float validation. A real process loads validated
            # settings (1.0); issue the new grant against that exact roundtrip.
            core.settings = type(core.settings).model_validate(core.settings.model_dump())
            purposes = {
                "chat_only": ["chat"],
                "no_meme": ["chat", "expression"],
                "no_expression": ["chat", "meme"],
            }.get(variant, grant.policy.purposes)
            policy = type(grant.policy).model_validate(
                {
                    **grant.policy.model_dump(),
                    "purposes": purposes,
                    "capability_policies": {"meme": meme_service.policy_hash(core.settings)}
                    if "meme" in purposes
                    else {},
                }
            )
            active, _ = activate(core, policy, revision=2, operation="activate-core-catalog")
            new_grant = SimpleNamespace(authority=active["authority"], key=grant.key, policy=policy)
            # This live HTTP server shares the original Settings reference. Reopen
            # it later for Core-mode endpoint checks; ingress itself is unchanged.
            import httpx

            from tests.support.core_http import real_core_http

            current = job = None
            if getattr(request, "param", None) == "no_current":
                service = MemeCaptionService(core.store, core.settings, None, core.clock)
                return SimpleNamespace(
                    core=core,
                    service=service,
                    retrieval=MemeRetrieval(service),
                    job=None,
                    source=event,
                    caption_id=caption["job_id"],
                    current=None,
                    asset_id=imported[0]["asset_id"],
                    data=data,
                    config=config,
                    grant=new_grant,
                    item=item,
                    custody=custody,
                )
            with Core(core.settings, clock=core.clock) as api_core:
                async with (
                    real_core_http(api_core) as port,
                    httpx.AsyncClient(
                        base_url=f"http://127.0.0.1:{port}",
                        trust_env=False,
                        headers={"Authorization": "Bearer " + config.bridge_token},
                    ) as current_client,
                ):
                    current, accepted = await admit(
                        current_client,
                        config,
                        new_grant,
                        now,
                        2,
                        text="合成闲聊，没有意图词" if variant == "no_match" else "想要温暖的抱抱",
                    )
                    assert accepted.status_code == 202, accepted.text
            with Core(core.settings, clock=core.clock) as claimant:
                job = claimant.claim_job("runtime-case")
            assert job is not None and job["event_id"] == current["event_id"]
            service = MemeCaptionService(core.store, core.settings, None, core.clock)
            return SimpleNamespace(
                core=core,
                service=service,
                retrieval=MemeRetrieval(service),
                job=job,
                source=event,
                caption_id=caption["job_id"],
                current=current,
                asset_id=imported[0]["asset_id"],
                data=data,
                config=config,
                grant=new_grant,
                item=item,
                custody=custody,
            )

    return run(setup())


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
        "SELECT 1 FROM job_sources WHERE job_id=? AND event_id=?",
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
        s.execute("UPDATE meme_captions SET result_json=NULL WHERE job_id=?", (c.caption_id,))
    elif change == "usage":
        s.execute("UPDATE model_usage SET state='UNKNOWN'")
    elif change == "epoch":
        from companion.privacy import advance_epoch

        advance_epoch(s)
    elif change == "binding":
        saved = c.retrieval.check(c.job)
        next(iter(saved["catalog_bindings"].values()))["source_sha256"] = "f" * 64
        s.execute(
            "UPDATE runtime_meta SET value=? WHERE key=?",
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
    row = c.core.store.one("SELECT body_json FROM events WHERE event_id=?", (c.job["event_id"],))
    value = json.loads(row["body_json"])
    c.core.store.execute(
        "UPDATE events SET body_json=? WHERE event_id=?",
        (encode({**value, "control_command": True}), c.job["event_id"]),
    )
    run(c.service.prepare_offer(c.job))
    c.service.check_offer(c.job)
    assert not c.core.store.one(
        "SELECT 1 FROM runtime_meta WHERE key=?", ("meme_offer:" + c.job["event_id"],)
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
        "UPDATE outbox SET content_json=?,state='SENT',sent_at=? WHERE message_id=?",
        (encode({"text": "sent", "image": {"candidate_id": sha}}), after(now, -1801), message),
    )
    assert not blocked(s, c.core.settings, sha, now)
    s.execute("UPDATE outbox SET sent_at=? WHERE message_id=?", (now, message))
    assert blocked(s, c.core.settings, sha, now)
    s.execute(
        "UPDATE outbox SET state='DELIVERY_UNCERTAIN',sent_at=NULL WHERE message_id=?", (message,)
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
        "UPDATE meme_assets SET state='REMOVED',data=NULL,mime=NULL,"
        "metadata_json=NULL WHERE asset_id=?",
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
    with c.core.store.transaction():
        c.core.ledger.expire_events(c.core.store, [c.source["event_id"]], c.core.clock())
    assert not c.core.store.one(
        "SELECT 1 FROM runtime_meta WHERE key=?", ("meme_offer:" + c.job["event_id"],)
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
        with closing(Store(c.core.settings.database)) as store:
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
                store.one("SELECT content_json FROM outbox WHERE message_id=?", (message,))[
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
    from tests.test_next_memes_animation import canvas_gif
    from tests.test_next_memes_catalog import png

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
        "UPDATE runtime_meta SET value=? WHERE key=?",
        (encode(saved), "meme_offer:" + c.job["event_id"]),
    )
    with pytest.raises(CoreError):
        c.service.check_offer(c.job)
    assert c.core.store.rows("SELECT * FROM model_usage") == before
    assert not c.core.store.rows("SELECT * FROM outbox")

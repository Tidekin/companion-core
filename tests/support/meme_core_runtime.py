import json
from types import SimpleNamespace

import pytest

from companion import meme_asset_migration, meme_policy, meme_service, owner_authority
from companion.core import Core
from companion.meme_core import CaptionBody, MemeCaptionService
from companion.meme_retrieval import MemeRetrieval
from companion.owner_authority_protocol import AuthorityStop
from companion.store import encode
from companion_meme_protocol import digest
from tests.conftest import run
from tests.meme_authority_fixture import admit
from tests.support.meme_core import body, caption_environment, worker
from tests.support.owner_authority import activate

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
                'SELECT asset_data FROM meme_captions WHERE job_id=%s', (caption["job_id"],)
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
                        # A one-character reply is neither cued nor an ambient light turn.
                        text={"no_match": "嗯", "ambient": "周末去公园走走",
                              "ambient_low": "今天好累"}.get(variant, "想要温暖的抱抱"),
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

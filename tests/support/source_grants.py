"""Shared synthetic source grants setup for component and process tests."""

from datetime import datetime, timedelta

from companion.exploration import ReadItem, ReadResult
from companion.privacy import epoch
from companion.source_grants import SourceGrantRegistration, SourceGrantSnapshot
from tests.support.exploration import configure

URL = "https://www.xiaoheihe.cn/app/bbs/link/168860244"


GRANT_ID = "grant_" + "1" * 32


def authorization(core, **changes):
    return SourceGrantSnapshot.model_validate(
        {
            "grant_id": GRANT_ID,
            "epoch": 1,
            "fingerprint": "a" * 64,
            "expires_at": (datetime.fromisoformat(core.clock()) + timedelta(hours=1)).isoformat(),
            **changes,
        }
    )


def prepare(core):
    configure(core)
    core.settings.observation_adapters["heybox"].source_grant_required = True
    snapshot = authorization(core)
    core.observations.grants.register(
        SourceGrantRegistration(adapter_id="heybox", **snapshot.snapshot())
    )
    return snapshot


def lease(core, snapshot, key="read-1"):
    with core.store.transaction():
        core.exploration.create("heybox", "read", key, URL)
    return core.exploration.lease("heybox", snapshot)


def receipt(item, snapshot):
    return ReadResult(
        lease_token=item["lease_token"],
        status="SUCCESS",
        used_bytes=400,
        used_requests=1,
        source_grant=snapshot,
        items=[ReadItem(title="Private fixture", text="Source-grant private fixture", url=URL)],
    )


def accepted(core, snapshot):
    item = lease(core, snapshot)
    result = core.exploration.complete("heybox", item["request_id"], receipt(item, snapshot))
    return result["event_ids"][0]


def derived_records(core, event_id):
    s, now = core.store, core.clock()
    with s.transaction():
        s.insert(
            "memories",
            memory_id="mem_grant_fixture",
            subject="self",
            kind="fact",
            content="Private derived memory",
            status="CONFIRMED",
            origin="event_observed",
            confidence=1,
            importance=0.5,
            privacy_scope="private",
            created_at=now,
            updated_at=now,
        )
        s.insert(
            "memory_evidence",
            memory_id="mem_grant_fixture",
            event_id=event_id,
            polarity=1,
            created_at=now,
        )
        s.insert(
            "memory_vectors",
            memory_id="mem_grant_fixture",
            model_version="fixture",
            content_hash="a" * 64,
            vector=b"\0" * 16,
        )
        s.insert(
            "diary_runs",
            run_id="diary_grant_fixture",
            identity_id="companion",
            local_date="2026-10-18",
            timezone="Asia/Shanghai",
            state="COMMITTED",
            max_attempts=1,
            created_at=now,
            updated_at=now,
        )
        s.insert(
            "diary_entries",
            entry_id="entry_grant_fixture",
            run_id="diary_grant_fixture",
            identity_id="companion",
            local_date="2026-10-18",
            timezone="Asia/Shanghai",
            title="Private diary",
            body_md="Private derived diary",
            status="COMMITTED",
            source_set_sha256="a" * 64,
            captured_privacy_epoch=epoch(s),
            generator_version="fixture",
            model_name="fixture",
            created_at=now,
            updated_at=now,
        )
        s.insert(
            "diary_evidence",
            entry_id="entry_grant_fixture",
            source_id="source_1",
            event_id=event_id,
            excerpt_sha256="a" * 64,
        )
        s.insert(
            "social_candidates",
            candidate_id="candidate_grant_fixture",
            event_id=event_id,
            canonical_url=URL,
            content_sha256="a" * 64,
            comparison_text="Private candidate",
            state="CANDIDATE",
            expires_at=now,
            created_at=now,
            updated_at=now,
        )

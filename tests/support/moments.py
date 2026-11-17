"""Moment fixtures through the real Life consumption or a seeded chat-authorized row."""

from companion.paid_budget import sha
from companion.store import encode, uid
from tests.support.life_expression_consumption import prepared


def moment_ready(core, tmp_path, **options):
    options.setdefault("purposes", ["moments"])
    return prepared(core, tmp_path, destination="PRIVATE_MOMENT",
                    life_purposes=["PRIVATE_MOMENT"], automatic_actions=["moment.save"],
                    **options)


def seed_moment(core, text):
    """A published row for read-side routes; publishing itself is tested via Life."""
    now = core.clock()
    job_id = uid("job")
    core.store.insert("jobs", job_id=job_id, kind="activity", state="SUCCEEDED",
                      idempotency_key="moment-fixture:" + job_id, created_at=now,
                      updated_at=now)
    moment_id = uid("moment")
    core.store.insert("moments", moment_id=moment_id, source_job_id=job_id, text=text,
                      content_sha256=sha({"text": text}), trigger="LIFE",
                      source_event_ids_json=encode([]), state="PUBLISHED", created_at=now,
                      updated_at=now)
    return moment_id

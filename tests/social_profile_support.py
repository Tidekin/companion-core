"""Native owner-confirmation fixture for profile-purpose tests."""

from companion.contracts import Candidate
from tests.conftest import ingest


def confirmed_profile_material(core, *, number=800, text="我偏爱星空与简洁的表达。",
                               subject="user", kind="preference", confirm=True):
    core.settings.memory_enabled = True
    origin = "onebot-direct:FriendMessage:" + core.settings.owner_qq_id
    source = ingest(core, number, text, origin=origin)
    with core.store.transaction():
        memory_id = core.memory.add(Candidate(
            subject=subject, kind=kind, fact_key="profile-preference-" + str(number), content=text,
            evidence_event_id=source["event_id"], evidence_quote=text,
        ), core.clock(), confirmed=False)
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (source["job_id"],))
    if confirm:
        decision = ingest(core, number + 1, "/确认这一条 " + memory_id, origin=origin)
        core.memory.decide(memory_id, "confirm", core.clock(),
                           evidence_event_id=decision["event_id"])
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s",
                           (decision["job_id"],))
    return memory_id

from companion.contracts import Candidate
from tests.conftest import ingest


def fact(core, number, text):
    core.settings.memory_enabled = True
    event = ingest(core, number, text)
    with core.store.transaction(control_write=True):
        memory_id = core.memory.add(
            Candidate(
                subject="project",
                kind="fact",
                fact_key=text,
                content=text,
                evidence_event_id=event["event_id"],
                evidence_quote=text,
            ),
            core.clock(),
            confirmed=True,
        )
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (event["job_id"],))
    return memory_id

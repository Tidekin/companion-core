from companion.contracts import Candidate
from tests.conftest import event


def remember(core, number, content, subject="user", fact_key=None, confirmed=False):
    core.settings.memory_enabled = True
    ev = event(core, number, content)
    core.ingest(ev, f"key-{number}")
    with core.store.transaction(control_write=True):
        return core.memory.add(
            Candidate(
                subject=subject,
                kind="preference",
                fact_key=fact_key or content,
                content=content,
                evidence_event_id=ev.event_id,
                evidence_quote=content,
            ),
            core.clock(),
            confirmed=confirmed,
        )

from companion import memory_references as refs
from companion.contracts import Candidate
from tests.conftest import ingest, run

TEXT = "青栎资料卡写着：入场日期是十二月九日，座位为B17。"


def prepared(core, kind="document"):
    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 1
    core.settings.extraction_idle_seconds = 0
    ingest(core, 1, "这是一份别人整理的资料")
    assert run(core.run_once())
    value = refs.capture(core.memory, "evt_test_1", kind, TEXT,
                         {"fixture": "native-reference-contract"}, core.clock())
    item = Candidate(subject="project", kind="fact", fact_key="visit", content=TEXT,
        evidence_event_id="evt_test_1", evidence_quote="入场日期是十二月九日",
        evidence_reference_id=value["reference_id"])
    return value, item

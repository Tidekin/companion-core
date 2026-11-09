"""SDK reads bind existing Diary entries without creating another diary run."""

import pytest

from companion.contracts import CoreError
from tests.support.diary import DAY, generate, seed_day


@pytest.mark.parametrize("change", ["entry", "source", "forget", "retention"])
def test_diary_context_proof_rejects_changed_or_withdrawn_material(core, change):
    seed_day(core)
    generate(core)
    entry = core.diary.get_day(DAY, core.settings.owner_qq_id)
    proof = core.diary.context_proof(entry)
    before = core.store.one("SELECT COUNT(*) n FROM diary_runs")["n"]
    core.diary.revalidate_context(proof)
    if change == "entry":
        core.store.execute("UPDATE diary_entries SET body_md='Changed' WHERE entry_id=%s",
                           (entry["entry_id"],))
    elif change == "source":
        core.store.execute('UPDATE events SET redacted=1 WHERE event_id=%s',
                           (proof["source_event_ids"][0],))
    elif change == "forget":
        core.diary.forget(DAY, core.settings.owner_qq_id)
    else:
        core.clock.advance((core.settings.raw_event_retention_days + 1) * 86400)
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        core.diary.revalidate_context(proof)
    assert core.store.one("SELECT COUNT(*) n FROM diary_runs")["n"] == before

"""Native source erasure and ledger replay remove derived mood appraisals."""

from cryptography.fernet import Fernet

from companion.backup import backup, restore
from companion.core import Core
from tests.support.mood import erase
from tests.support.mood_appraisals import appraise


def test_event_cleanup_registered_for_appraisals(core):
    key = appraise(core, 1, "测试遗忘")
    assert core.store.one("SELECT event_id FROM mood_appraisals")["event_id"] == key
    erase(core, [key])
    assert core.store.rows("SELECT * FROM mood_appraisals") == []


def test_appraisal_removed_on_event_erasure_preserves_others(core):
    first = appraise(core, 1, "第一条")
    second = appraise(core, 2, "第二条", "OWNER_PLAYFUL")
    erase(core, [first])
    assert core.store.rows("SELECT event_id FROM mood_appraisals") == [{"event_id": second}]


def test_erasure_removes_appraisal_from_projection(core):
    key = appraise(core, 1, "开玩笑", "OWNER_PLAYFUL")
    assert len(core.mood.snapshot()["signals"]) == 1
    erase(core, [key])
    assert core.mood.snapshot()["signals"] == []


def test_multiple_erasures_handle_appraisals_correctly(core):
    keys = [appraise(core, number, f"文本{number}") for number in range(1, 6)]
    for key in keys[::2]:
        erase(core, [key])
    assert core.store.rows("SELECT event_id FROM mood_appraisals ORDER BY event_id") == [
        {"event_id": keys[1]}, {"event_id": keys[3]},
    ]


def test_erasure_replay_removes_appraisal_from_an_old_backup(core, tmp_path):
    key = appraise(core, 1, "请记得我")
    secret = Fernet.generate_key()
    archive, restored_db = tmp_path / "before.fernet", tmp_path / "restored.db"
    backup(core.settings.database, archive, secret)
    erase(core, [key])
    restore(archive, restored_db, secret, core.settings.erasure_ledger)
    with Core(core.settings.model_copy(update={"database": str(restored_db)}),
              core.model, core.clock) as restored:
        assert restored.store.rows("SELECT * FROM mood_appraisals") == []
        assert restored.mood.snapshot()["signals"] == []

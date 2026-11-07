"""A database that once opened a legacy window must not fence later native chat."""

from companion import live_text, private_life
from companion.store import encode
from tests.owner_model_fixtures import authorized_job
from tests.support.paid_model import approved_live


def test_stopped_legacy_binding_does_not_apply_window_checks_to_native_chat(core, tmp_path):
    approved_live(core, tmp_path)
    plan = "a" * 64
    # What a pre-cutover database retains after its last window was stopped.
    core.store.insert("runtime_meta", key=live_text.BINDING, value=encode({
        "plan_sha256": plan, "window_id": "win_" + "b" * 32}))
    core.store.insert("runtime_meta", key=live_text.STOP + plan, value="OWNER_STOP")
    job_id = authorized_job(core)
    job = core.store.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
    assert core.settings.live_text_window is None
    assert not private_life.strict(core.store, core.settings)
    core._check_context_snapshots(dict(job), [])

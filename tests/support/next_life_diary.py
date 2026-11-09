from tests.support.diary import DAY, generate, seed_day


def seed(core, count=4):
    model = seed_day(core, [f"今天讨论了读书计划的第 {i} 步。" for i in range(count)])
    assert generate(core)["state"] == "COMMITTED"
    return model


def withdraw(core, event_id="evt_test_1"):
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, [event_id], core.clock())
    assert core.diary.get_day(DAY, core.settings.owner_qq_id) is None

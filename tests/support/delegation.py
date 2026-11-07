from companion import delegation


def delegate(core, capability="wake", mode="self", allow_pause=True, ranges=None):
    with core.store.transaction():
        return delegation.set_delegation(core.store, core.settings, capability, mode, allow_pause,
                                         ranges if ranges is not None else {}, core.clock())


def submit(core, field, value, capability="wake"):
    with core.store.transaction():
        return delegation.submit(core.store, core.settings, capability, field, value, "理由",
                                 core.clock())

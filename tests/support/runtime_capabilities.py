from companion.capabilities import snapshot


def rows(core, **kwargs):
    return {row["id"]: row for row in snapshot(core.store, core.settings, core.clock, **kwargs)
            ["capabilities"]}

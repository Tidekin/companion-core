from companion.memory_context import MemoryContext


def context(core):
    core.settings.memory_enabled = True
    return MemoryContext(core.memory)


def episode(core, *numbers):
    events = [core.store.one("SELECT * FROM events WHERE event_id=?", (f"evt_test_{n}",))
              for n in numbers]
    return core.memory.episodes(events, core.clock())

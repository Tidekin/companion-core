from companion import live_text
from tests.support.memory_commands import fact


def scope_facts(core):
    memory_id = fact(core, 900, "本次临时项目的验收代号是青竹七号。")
    outsider = fact(core, 901, "未批准的另一个项目代号是紫云三号。")
    return (
        memory_id,
        outsider,
        [
            live_text.memory_fact(
                core.store,
                core.settings,
                core.clock,
                memory_id,
            )
        ],
    )

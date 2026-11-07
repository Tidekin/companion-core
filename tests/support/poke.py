from companion import poke
from tests.poke_authority_fixture import signed


def native_frame(now=1792382400, **changes):
    return {"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
            "time": now, "self_id": 10002, "user_id": 10001, "sender_id": 10001,
            "target_id": 10002, "action": "拍了拍", "suffix": "", **changes}


def receive(env, **changes):
    return poke.receive(env.store, env.settings, env.clock, signed(env, **changes))


def claim(env):
    env.clock.advance(2)
    return poke.claim_ready(env.store, env.settings, env.clock, "poke-worker")

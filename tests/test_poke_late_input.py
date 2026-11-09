"""A capture arriving at the SDK HTTP boundary still preempts a claimed poke."""

import httpx

from companion import paid_budget, poke
from companion.model import ModelClient
from tests.conftest import run
from tests.poke_authority_fixture import environment, owner_turn, signed


def test_late_signed_input_preempts_poke_without_provider_io_or_retry(core, tmp_path):
    env = environment(core, tmp_path)
    capture = signed(env)
    poke.receive(core.store, core.settings, core.clock, capture)
    core.clock.advance(core.settings.poke_policy.coalesce_seconds + 1)
    calls, inputs = [], []

    async def before_http(request):
        assert request.url.host == "synthetic.invalid"
        inputs.append(owner_turn(env, 2, "先看这一条", approve_only=True))

    def provider(request):
        calls.append(request)
        raise AssertionError("new owner input must prevent provider dispatch")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider),
            event_hooks={"request": [before_http]}) as client:
            core.model = ModelClient(core.settings, client)
            assert await core._run_poke_once("late-input-poke")
            assert not await core._run_poke_once("late-input-poke-restart")

    run(scenario())
    assert len(inputs) == 1 and calls == []
    assert not core.store.one('SELECT 1 FROM events WHERE event_id=%s',
                               (inputs[0]["event_id"],))
    assert core.store.one("SELECT state,last_error_code FROM jobs WHERE kind='poke'") == {
        "state": "FAILED", "last_error_code": "BACKGROUND_YIELD_TO_CHAT"}
    assert core.store.one("SELECT state,scenario,billed_micro_usd,input_tokens,output_tokens "
                          "FROM model_usage") == {
        "state": "SETTLED", "scenario": "qq_poke", "billed_micro_usd": 0,
        "input_tokens": 0, "output_tokens": 0}
    assert core.store.one("SELECT COUNT(*) n FROM model_usage")["n"] == 1
    assert core.store.one("SELECT COUNT(*) n FROM audit_log "
                          "WHERE action='model.not_dispatched'")["n"] == 1
    assert core.store.rows("SELECT * FROM outbox") == []
    paid_budget.journal(core.store, core.settings.paid_model)

from companion import budget


def assert_settled_calls(core, count, *, scenario="chat"):
    charged = budget.cost(core.settings, 100, 20, scenario=scenario)
    assert charged > 0
    assert core.store.rows(
        "SELECT scenario,state,input_tokens,output_tokens,billed_micro_usd FROM model_usage"
    ) == [{"scenario": scenario, "state": "SETTLED", "input_tokens": 100,
           "output_tokens": 20, "billed_micro_usd": charged}] * count

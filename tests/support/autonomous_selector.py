import pytest


async def drain(selector, limit=100):
    turns = []
    for _ in range(limit):
        selected = await selector.step()
        turns.append(selected)
        if not selected.more:
            return turns
    pytest.fail("native selector did not finish its bounded idle sweep")

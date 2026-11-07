import asyncio

import pytest

from tests.support.next_memes_catalog import check, png
from tests.support.next_memes_discovery import async_check
from tests.support.next_memes_semantic import MockEmbedder, add, ready
from tests.support.next_memes_semantic import semantic as semantic
from tests.support.next_memes_semantic_quality import CASES, check_caption_fixture_quality


@pytest.mark.parametrize("name,text,intent,target,expected", CASES, ids=[c[0] for c in CASES])
def test_fixed_caption_fixture_quality(semantic, name, text, intent, target, expected):
    return check_caption_fixture_quality(semantic, name, text, intent, target, expected)


def test_embedding_adds_one_nonlexical_positive_without_recaptioning(semantic):
    store, client = semantic
    sha = add(semantic)
    ready(semantic, sha)
    request = {"text": "温暖鼓励", "intent": "comfort", "target": "recipient"}
    assert store.candidates("lexical", request, check=check) == []
    embedder = MockEmbedder()
    asyncio.run(store.index(sha, embedder, check=async_check, commit_check=check))
    vector = asyncio.run(embedder.embed((request["text"],)))
    result = store.candidates("embedded", request, query_vector=vector, check=check)
    assert [row["candidate_id"] for row in result] == [sha]
    assert len(client.model_calls) == 1


def test_frozen_top_eight_no_new_candidate_or_context_on_replay(semantic):
    store, client = semantic
    for value in range(10):
        sha = add(semantic, png((value, 10, 10)))
        ready(semantic, sha)
    request = {"text": "想要抱抱", "intent": "comfort", "target": "recipient"}
    offers = store.candidates("bounded", request, check=check)
    assert len(offers) == 8
    smaller = store.candidates("small", request, limit=2, check=check)
    assert store.candidates("small", request, limit=8, check=check) == smaller
    all_ids = {row[0] for row in store.db.execute("SELECT sha FROM meme_assets")}
    unoffered = next(iter(all_ids - {row["candidate_id"] for row in offers}))
    assert store.catalog.prepare_selection("bounded", unoffered, check=check) is None
    with pytest.raises(ValueError, match="OFFER_CONFLICT"):
        store.candidates("bounded", {**request, "target": "self"}, check=check)
    assert len(client.model_calls) == 10

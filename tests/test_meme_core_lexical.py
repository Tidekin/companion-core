"""Compare the original fixed caption/GIF corpus in explicitly vectorless mode."""

import pytest

from companion.meme_policy import lexical_score
from tests.support.next_memes_semantic import semantic as semantic
from tests.support.next_memes_semantic_quality import CASES
from tests.support.next_memes_semantic_quality import check_caption_fixture_quality as bridge_corpus


@pytest.mark.parametrize("name,text,intent,target,expected", CASES, ids=[c[0] for c in CASES])
def test_core_lexical_ranks_match_original_caption_corpus(
    semantic,
    name,
    text,
    intent,
    target,
    expected,
):
    # Build the unchanged reviewed positive/negative/GIF fixtures and check their
    # original expected labels before comparing Core's actual lexical score.
    bridge_corpus(semantic, name, text, intent, target, expected)
    store, _ = semantic
    query = {"text": text, "intent": intent, "target": target}
    bridge = store.candidates(name, query, check=lambda: None)
    ranks = []
    for (sha,) in store.db.execute("SELECT sha FROM meme_assets WHERE active=1 ORDER BY sha"):
        value = store.caption(sha)
        if value:
            score = lexical_score(value[0], query)
            if score:
                ranks.append((score, sha))
    ranks.sort(key=lambda row: (-row[0], row[1]))
    assert [sha for _, sha in ranks[:8]] == [candidate["candidate_id"] for candidate in bridge]

from companion import style_candidates
from tests.support.bili_style_candidates import GOOD, corpus, live, pair_rows
from tests.support.bili_style_corpus import NOW


def approved(pattern=GOOD, rows=None):
    c = corpus(*(rows or [pair_rows()]))
    cand = style_candidates.distill(c, NOW, live(c), lambda pair: pattern)[0][0]
    return style_candidates.review(cand, reviewer="主人", approve=True, reason="",
                                   approved_versions={}), live(c)

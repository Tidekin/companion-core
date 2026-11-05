"""Offline benchmark of the meme candidate stage; no model, network or delivery calls.

Raw owner text goes through the production query parser and candidate rules against a
labelled caption library. It measures whether an acceptable sticker reaches the offer and
whether refused or unsuitable contexts leak into it. Whether the chat model then picks
the right one, and whether QQ delivers it, need a real-model run and are not measured.
With ``--embeddings`` the local BGE weights add cosine scores and a pair-level summary
that can inform the cosine threshold for this library.
"""

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from companion import meme_policy
from companion.meme_index import PROFILE
from companion.meme_retrieval import query_for
from companion_meme_protocol import caption_text

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "tests" / "fixtures" / "meme_quality_cases.json"
CAPTION_DEFAULTS = {"confidence": 0.95, "content_status": "clear", "temporal_status": "static"}
EXPECTATIONS = {"must_offer", "may", "must_not", "must_not_celebrate"}


def library(value):
    stickers = {}
    for label, fields in value.items():
        caption = SimpleNamespace(**{**CAPTION_DEFAULTS, **fields})
        for key in ("suitable", "avoid", "use_when", "avoid_when", "tags"):
            setattr(caption, key, tuple(getattr(caption, key)))
        stickers[label] = caption
    return stickers


def load(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    stickers = library(value["library"])
    for case in value["cases"]:
        unknown = set(case["acceptable"]) | set(case["unacceptable"])
        if case["expect"] not in EXPECTATIONS or not unknown <= set(stickers):
            raise ValueError("MEME_BENCHMARK_CASE_INVALID: " + case["name"])
    return value, stickers


def candidates(text, stickers, *, similarity=None, threshold=PROFILE["cosine_threshold"],
               limit=PROFILE["offer_limit"]):
    """Same gates and scoring as MemeRetrieval.prepare, over the supplied library only."""
    query = query_for(text)
    if not query["intent"]:
        return query, []
    scored = []
    for label, caption in stickers.items():
        if not meme_policy.caption_eligible(caption) or not meme_policy.context_allows(
            caption, query
        ):
            continue
        score = meme_policy.lexical_score(caption, query)
        cosine = similarity(query["text"], label) if similarity else None
        if cosine is not None and cosine >= threshold:
            score += cosine
        if score > 0:
            scored.append((-score, label))
    return query, [label for _, label in sorted(scored)[:limit]]


def ratio(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "rate": round(numerator / denominator, 4) if denominator else None}


def evaluate(value, stickers, **options):
    rows, recall5, recall8 = [], [0, 0], [0, 0]
    completed, leaked, misfired, unacceptable = [0, 0], [0, 0], [0, 0], [0, 0]
    for case in value["cases"]:
        query, offered = candidates(case["text"], stickers, **options)
        good, bad = set(case["acceptable"]), set(case["unacceptable"])
        if good:
            recall5[1] += 1
            recall8[1] += 1
            recall5[0] += bool(good & set(offered[:5]))
            recall8[0] += bool(good & set(offered[:8]))
        if case["expect"] == "must_offer":
            completed[1] += 1
            completed[0] += bool(good & set(offered))
        if case["expect"] == "must_not":
            leaked[1] += 1
            leaked[0] += bool(offered)
        if case["expect"] == "must_not_celebrate":
            misfired[1] += 1
            misfired[0] += bool(bad & set(offered))
        if offered:
            unacceptable[1] += 1
            unacceptable[0] += bool(bad & set(offered))
        rows.append({"name": case["name"], "group": case["group"], "intent": query["intent"],
                     "target": query["target"], "request": query["request"],
                     "offered": offered, "acceptable_hit": sorted(good & set(offered)),
                     "unacceptable_hit": sorted(bad & set(offered))})
    return {
        "schema_version": 1, "benchmark": value["name"], "scope": "candidate_stage_only",
        "cases": len(value["cases"]), "library": len(stickers),
        "metrics": {
            "explicit_request_candidate_rate": ratio(*completed),
            "candidate_recall_at_5": ratio(*recall5),
            "candidate_recall_at_8": ratio(*recall8),
            "refused_context_offer_rate": ratio(*leaked),
            "negated_or_ironic_misfire_rate": ratio(*misfired),
            "offers_with_unacceptable_candidate": ratio(*unacceptable),
        },
        "rows": rows,
    }


def embedding_similarity(cache_dir, stickers):
    from companion.retrieval import encoder

    model = encoder(cache_dir, local_only=True)
    labels = list(stickers)
    passages = list(model.passage_embed([caption_text(stickers[k]) for k in labels]))
    vectors = dict(zip(labels, passages, strict=True))
    queries = {}

    def similarity(text, label):
        if text not in queries:
            queries[text] = next(iter(model.query_embed(text)))
        left, right = queries[text], vectors[label]
        dot = sum(a * b for a, b in zip(left, right, strict=True))
        norm = (sum(a * a for a in left) * sum(b * b for b in right)) ** 0.5
        return float(dot / norm)

    return similarity


def pair_summary(value, similarity):
    """Cosine of labelled pairs; a hint for this library, not a universal threshold."""
    good, bad = [], []
    for case in value["cases"]:
        text = query_for(case["text"])["text"]
        good += [similarity(text, label) for label in case["acceptable"]]
        bad += [similarity(text, label) for label in case["unacceptable"]]
    best = None
    for step in range(30, 91):
        threshold = step / 100
        tp = sum(v >= threshold for v in good)
        fp = sum(v >= threshold for v in bad)
        precision = tp / (tp + fp) if tp + fp else 0
        recall = tp / len(good) if good else 0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
        if best is None or f1 > best["f1"]:
            best = {"threshold": threshold, "f1": round(f1, 4),
                    "precision": round(precision, 4), "recall": round(recall, 4)}
    return {"acceptable_pairs": len(good), "unacceptable_pairs": len(bad),
            "acceptable_mean": round(sum(good) / len(good), 4) if good else None,
            "unacceptable_mean": round(sum(bad) / len(bad), 4) if bad else None,
            "best_f1_threshold": best}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--embeddings", metavar="CACHE_DIR",
                        help="score with local BGE weights from this cache directory")
    parser.add_argument("--rows", action="store_true", help="include per-case rows")
    args = parser.parse_args(argv)
    value, stickers = load(args.cases)
    similarity = embedding_similarity(args.embeddings, stickers) if args.embeddings else None
    report = evaluate(value, stickers, similarity=similarity)
    if similarity:
        report["cosine_pairs"] = pair_summary(value, similarity)
    if not args.rows:
        report.pop("rows")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

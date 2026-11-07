"""Enforce the campaign's existing corpus, answer and grading freezes offline."""

import hashlib
import json
from pathlib import Path

from roundcfg import sha


def answer_freeze(r):
    path = r.root / "all-answers-frozen.json"
    frozen = json.loads(path.read_text("utf-8"))
    assert frozen["status"] == "ALL_ANSWERS_CLOSED_BEFORE_GOLD_READ", "Answer freeze not closed"
    assert frozen["answers"] == r.answers_total, "Answer freeze count changed"
    files = []
    for arm in r.graded_arms:
        files += ([r.root / arm / "answers.jsonl"] if arm == "rag" or arm in r.controls else
                  sorted((r.arm_dir(arm) / "results").glob("*/answers.jsonl")))
    assert set(frozen["files_sha256"]) == {p.relative_to(r.root).as_posix() for p in files}, \
        "Answer freeze file inventory changed"
    for name, digest in frozen["files_sha256"].items():
        assert sha(r.root / name) == digest, "Answer freeze changed: " + name
    corpus_path = r.root / "corpus-freeze.json"
    assert sha(corpus_path) == frozen["corpus_freeze_sha256"], "Corpus freeze changed"
    corpus = json.loads(corpus_path.read_text("utf-8"))
    bundle_path = r.bank / "bundle-manifest.json"
    assert sha(bundle_path) == corpus["bundle_manifest_sha256"], "Corpus freeze changed: bundle manifest"
    bundle = json.loads(bundle_path.read_text("utf-8"))["files_sha256"]
    for name, digest in bundle.items():
        assert sha(r.bank / name) == digest, "Corpus freeze changed: " + name
    expected_inputs = {name for name in bundle if Path(name).parent.as_posix() == "suite-v1/inputs"
                       and name.endswith(".jsonl")}
    assert expected_inputs == {p.relative_to(r.bank).as_posix() for p in (r.suite / "inputs").glob("*.jsonl")}, \
        "Corpus freeze input inventory changed"
    for name, field in (("questions.jsonl", "questions_sha256"),
                        ("review-only/criteria.jsonl", "criteria_sha256")):
        assert sha(r.suite / name) == corpus[field], "Corpus freeze changed: " + name
    return sha(path)


def vote_settings(r, votes=None):
    g = r.cfg["grading"]
    votes = votes if votes is not None else len(g["judges"]) if g["judges"] else g["votes"]
    judges = list(g["judges"] or ["deepseek"] * votes)
    assert type(votes) is int and votes > 0 and len(judges) == votes
    assert set(judges) <= set(r.cfg["judges"]), judges
    return votes, judges


def grading_identity(r, version, rules, votes=None):
    """The resolved CLI vote count is frozen as well as round.json settings."""
    votes, judges = vote_settings(r, votes)
    g = r.cfg["grading"]
    return dict(
        **{k: v for k, v in g.items() if k != "rubric"}, rubric=version,
        effective_votes=votes, effective_judges=judges,
        judge_configs={name: r.cfg["judges"][name] for name in set(judges)},
        rules_sha256=hashlib.sha256(rules.encode()).hexdigest(),
        answers_freeze_sha256=answer_freeze(r),
        criteria_sha256=sha(r.suite / "review-only/criteria.jsonl"), total=r.answers_total,
        anchors_sha256=sha(Path(g["anchors"])) if g["anchors"] else None)


def check_grading_freeze(frozen, expected):
    changed = [k for k, v in expected.items() if k not in frozen or frozen[k] != v]
    assert not changed, ("Grading freeze changed: " + ", ".join(changed)
                         + "; preserve this archive and grade into a fresh --out directory")

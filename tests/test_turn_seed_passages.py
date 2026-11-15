"""Second-hop queries retain exact trailing relations, not only document prefixes."""

import hashlib
from types import SimpleNamespace

import pytest

from companion import turn_recall


@pytest.mark.parametrize("separator", ["。", "\n", ""])
def test_keyholder_after_long_background_reaches_exact_source_seed(separator):
    doc = "[2026-11-01] " + ("背景描述" + separator) * 65 + "钥匙现在由林澄保管。"
    doc += "\n你当时回复：钥匙由另外一个人保管。"
    parts = turn_recall.seed_passages(doc, "钥匙由谁保管")
    assert "林澄" in "".join(part["text"] for part in parts)
    assert all(doc[part["start"]:part["end"]] == part["text"] for part in parts)
    assert "另外一个人" not in turn_recall._evidence(doc, "钥匙由谁保管")
    assert sum(len(part["text"]) for part in parts) + len(parts) - 1 <= 160


def test_second_query_uses_question_focused_relation_without_a_model(monkeypatch):
    docs = ["背景。" * 70 + "钥匙由林澄保管。", "林澄周六在南门值班。"]
    queries = []

    def fused(settings, query, values, **kwargs):
        queries.append(query)
        return [0, 1]

    monkeypatch.setattr(turn_recall, "_fused", fused)
    settings = SimpleNamespace(turn_recall_rerank=False, turn_recall_second_hop=2)
    turn_recall.parts(settings, "谁保管钥匙", docs)
    assert len(queries) == 2 and "钥匙由林澄保管" in queries[1]


@pytest.mark.parametrize("change", [None, "revision", "withdraw"])
def test_changed_or_withdrawn_seed_cannot_nominate_second_hop(core, monkeypatch, change):
    core.settings.turn_recall_limit = 2
    core.settings.turn_recall_second_hop = 1
    rows = {key: {"event_id": key, "record_sequence": index, "user_text": text,
                  "reply_text": "好", "body_json": None,
                  "occurred_at": "2026-05-01T01:00:00+00:00"}
            for index, (key, text) in enumerate([
                ("seed", "钥匙由林澄保管"), ("decoy", "另一个人的同名项目"),
                ("hop", "林澄周六在南门值班")])}
    digest = hashlib.sha256(turn_recall._document(rows["seed"], [],
                                                 core.settings.timezone).encode()).hexdigest()
    if change == "revision":
        rows["seed"]["user_text"] = "钥匙已转给别人，先前保管人失效"
    reads = []

    def candidates(store, settings, job, ids=None):
        reads.append(list(ids))
        return [rows[key] for key in ids], [[] for _ in ids]

    monkeypatch.setattr(turn_recall, "_candidates", candidates)
    ranking = {"first": ["seed", "decoy"], "hop": ["hop"], "seeds": [
        {"event_id": "seed", "document_sha256": digest, "passages": [{"start": 0, "end": 8}]}]}
    result = turn_recall.reference(core.store, core.settings,
        {"event_id": "current", "conversation_id": "conv"}, "钥匙", set(),
        lambda ids: change != "withdraw" or "seed" not in ids,
        max_turns=2, max_bytes=32000, ranked_ids=ranking)
    assert ("hop" in result["event_ids"]) is (change is None)
    assert bool(result.get("retrieval_seeds")) is (change is None)
    if change == "withdraw":
        assert all("seed" not in ids for ids in reads)

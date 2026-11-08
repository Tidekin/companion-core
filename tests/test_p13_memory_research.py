import copy
import hashlib

import pytest

from companion.contracts import Candidate, CoreError
from companion.memory import Memory
from scripts.p13_memory_research import LIMIT, MAX_ADDITIONS, mentions, retrieve
from tests.conftest import ingest


def fact(memory, text, source, now, *, subject="user"):
    return memory.add(Candidate(subject=subject, kind="fact", fact_key=hashlib.sha256(
        text.encode()).hexdigest(), content=text, evidence_event_id=source,
        evidence_quote=text), now, confirmed=True)


def setup(core):
    memory = Memory(core.store, core.settings.model_copy(update={"memory_enabled": True}),
                    core.ledger)
    first = "绿盒的领取点在东门。绿盒的清单放在布袋。"
    second = "绿盒的清单放在布袋。绿盒的归还处在西门。"
    sources = [ingest(core, index, text)["event_id"]
               for index, text in enumerate((first, second), 1)]
    a = fact(memory, "绿盒的领取点在东门。", sources[0], core.clock())
    b = fact(memory, "绿盒的清单放在布袋。", sources[0], core.clock())
    assert fact(memory, "绿盒的清单放在布袋。", sources[1], core.clock()) == b
    c = fact(memory, "绿盒的归还处在西门。", sources[1], core.clock())
    annotation = {"canonical": "绿盒", "aliases": ["绿盒"], "subject": "user",
                  "source_event_id": sources[0], "source_sha256": hashlib.sha256(
                      first.encode()).hexdigest(), "evidence_quote": first}
    return memory, [a, b, c], [annotation]


def test_one_hop_does_not_recurse_through_second_shared_source(core):
    memory, (a, b, c), annotations = setup(core)
    rows, additions = retrieve(memory, "领取点", "bounded_tags_onehop", annotations,
                               core.clock(), seeds=[a])
    assert {row["memory_id"] for row in rows} == {a, b}
    assert c not in {row["memory_id"] for row in rows}
    assert [item["kind"] for item in additions] == ["one_hop_same_owner_source"]
    assert additions[0]["edge_source_sha256"] == [annotations[0]["source_sha256"]]


def test_a_common_tag_without_shared_source_or_subject_does_not_create_an_edge(core):
    memory, (a, b, _), annotations = setup(core)
    text = "绿盒的图案印在海报上。"
    source = ingest(core, 3, text)["event_id"]
    other = fact(memory, text, source, core.clock())
    cross_subject = fact(memory, "绿盒的清单放在布袋。",
                         annotations[0]["source_event_id"], core.clock(), subject="project")
    rows, _ = retrieve(memory, "领取点", "bounded_tags_onehop", annotations,
                       core.clock(), seeds=[a])
    assert {row["memory_id"] for row in rows} == {a, b}
    assert not {other, cross_subject} & {row["memory_id"] for row in rows}


def test_alias_normalization_is_bounded_and_requires_exact_source_quote(core):
    assert mentions("ＲＱ下一步", "RQ")
    assert not mentions("FIRST_RQ_SECOND", "RQ")
    memory, _, annotations = setup(core)
    invalid = copy.deepcopy(annotations)
    invalid[0]["aliases"] = ["not-in-owner-source"]
    with pytest.raises(ValueError, match="UNSUPPORTED_RESEARCH_ANNOTATION"):
        retrieve(memory, "not-in-owner-source", "bounded_tags_onehop", invalid,
                 core.clock(), seeds=[])
    rows, additions = retrieve(memory, "绿盒", "bounded_tags_onehop", annotations,
                               core.clock(), seeds=[])
    assert len(rows) == len(additions) == MAX_ADDITIONS


@pytest.mark.parametrize("mutation", ["source_changed", "source_forgotten", "source_expired"])
def test_derived_annotations_are_revalidated_even_when_the_old_input_is_retained(core, mutation):
    memory, (a, _, _), annotations = setup(core)
    source = annotations[0]["source_event_id"]
    if mutation == "source_changed":
        core.store.execute("UPDATE events SET body_json='{}' WHERE event_id=%s", (source,))
    elif mutation == "source_forgotten":
        with core.store.transaction(control_write=True):
            core.ledger.expire_events(core.store, [source], core.clock())
    else:
        core.clock.advance(86400 * (memory.settings.raw_event_retention_days + 1))
    rows, additions = retrieve(memory, "绿盒", "bounded_tags_onehop", annotations,
                               core.clock(), seeds=[])
    assert rows == additions == []
    if mutation != "source_changed":
        rows, _ = retrieve(memory, "领取点", "bounded_tags_onehop", annotations,
                           core.clock(), seeds=[a])
        assert not rows


def test_unapplied_latest_ledger_stops_the_experiment_before_candidate_return(core, monkeypatch):
    memory, (a, _, _), annotations = setup(core)

    def interrupted(*args):
        raise RuntimeError("synthetic DB apply interruption")

    with monkeypatch.context() as patch:
        patch.setattr(core.ledger, "apply", interrupted)
        with pytest.raises(RuntimeError):
            with core.store.transaction(control_write=True):
                core.ledger.expire_events(core.store, [annotations[0]["source_event_id"]],
                                          core.clock())
    with pytest.raises(CoreError, match="ERASURE_REPLAY_REQUIRED"):
        retrieve(memory, "绿盒", "bounded_tags_onehop", annotations, core.clock(), seeds=[a])


def test_existing_six_candidates_leave_no_room_for_alias_expansion(core):
    memory, ids, annotations = setup(core)
    for index in range(3, LIMIT):
        text = f"独立合成记录编号是{index}。"
        source = ingest(core, index + 1, text)["event_id"]
        ids.append(fact(memory, text, source, core.clock()))
    rows, additions = retrieve(memory, "绿盒", "bounded_tags_onehop", annotations,
                               core.clock(), seeds=ids)
    assert len(rows) == LIMIT and additions == []

"""Test standing_relationship method in Memory."""
from companion.contracts import Candidate
from .conftest import ingest, event


def test_standing_relationship_confirmed(core):
    """Should return confirmed relationship memories with bilateral form."""
    core.settings.memory_enabled = True

    # Insert a confirmed relationship memory
    result = ingest(core, number=1, text="她叫我爸爸了，我也把她当女儿看")
    event_id = result["event_id"]

    candidate = Candidate(
        subject="relationship",
        kind="fact",
        fact_key="父女关系",
        content="对方叫你爸爸,也把你当作女儿般疼爱",
        evidence_event_id=event_id,
        evidence_quote="她叫我爸爸了，我也把她当女儿看",
        salience=9
    )
    memory_id = core.memory.add(candidate, now=core.clock(), confirmed=True)

    # Call the new method
    result = core.memory.standing_relationship(now=core.clock(), actor="owner")

    # Should have one entry
    assert len(result) == 1
    assert result[0]["memory_id"] == memory_id
    assert result[0]["subject"] == "relationship"
    assert result[0]["content"] == candidate.content


def test_standing_relationship_hypothesis_excluded(core):
    """Hypothesis memories should not appear in standing relationship."""
    core.settings.memory_enabled = True

    result = ingest(core, number=2, text="也许我们可以试试父女关系")
    event_id = result["event_id"]

    candidate = Candidate(
        subject="relationship",
        kind="fact",
        fact_key="关系假设",
        content="对方提议尝试父女关系",
        evidence_event_id=event_id,
        evidence_quote="也许我们可以试试父女关系",
        salience=5
    )
    # Save as hypothesis (confirmed=False)
    core.memory.add(candidate, now=core.clock(), confirmed=False)

    # Call the method
    result = core.memory.standing_relationship(now=core.clock(), actor="owner")

    # Should be empty
    assert result == []


def test_standing_relationship_forgotten_excluded(core):
    """Forgotten memories should not appear."""
    core.settings.memory_enabled = True

    result = ingest(core, number=3, text="她是我女儿")
    event_id = result["event_id"]

    candidate = Candidate(
        subject="relationship",
        kind="fact",
        fact_key="女儿",
        content="对方认为你们是父女关系",
        evidence_event_id=event_id,
        evidence_quote="她是我女儿",
        salience=8
    )
    memory_id = core.memory.add(candidate, now=core.clock(), confirmed=True)

    # Manually set to FORGOTTEN (bypasses decide's validation)
    with core.store.transaction():
        core.store.execute(
            "UPDATE memories SET status='FORGOTTEN', content='' WHERE memory_id=?",
            (memory_id,)
        )

    # Should be empty
    result = core.memory.standing_relationship(now=core.clock(), actor="owner")
    assert result == []


def test_standing_relationship_superseded_excluded(core):
    """Superseded memories should not appear."""
    core.settings.memory_enabled = True

    # First memory
    r1 = ingest(core, number=4, text="她是我女儿")
    ev1 = r1["event_id"]

    candidate1 = Candidate(
        subject="relationship",
        kind="fact",
        fact_key="关系",
        content="对方认为你们是父女关系",
        evidence_event_id=ev1,
        evidence_quote="她是我女儿",
        salience=8
    )
    memory_id_old = core.memory.add(candidate1, now=core.clock(), confirmed=True)

    # Superseding memory
    r2 = ingest(core, number=5, text="我们还是做朋友吧")
    ev2 = r2["event_id"]

    candidate2 = Candidate(
        subject="relationship",
        kind="fact",
        fact_key="关系",
        content="你们现在是朋友关系",
        evidence_event_id=ev2,
        evidence_quote="我们还是做朋友吧",
        salience=8
    )
    memory_id_new = core.memory.add(candidate2, now=core.clock(), confirmed=True,
                                    resolution={"conflicts": [memory_id_old]})

    # Manually mark the old one as SUPERSEDED (simulate user accepting the conflict resolution)
    with core.store.transaction():
        core.store.execute(
            "UPDATE memories SET status='SUPERSEDED', supersedes_memory_id=NULL WHERE memory_id=?",
            (memory_id_old,)
        )
        core.store.execute(
            "UPDATE memories SET supersedes_memory_id=?, needs_review=0 WHERE memory_id=?",
            (memory_id_old, memory_id_new)
        )

    # Only new one should appear
    result = core.memory.standing_relationship(now=core.clock(), actor="owner")
    assert len(result) == 1
    assert result[0]["memory_id"] == memory_id_new
    assert result[0]["memory_id"] == memory_id_new

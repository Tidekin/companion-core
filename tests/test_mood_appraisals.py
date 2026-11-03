"""Model-judged interaction signals from owner's natural language."""

from companion import mood_appraisals
from companion.contracts import InteractionSignal
from tests.conftest import ingest
from tests.test_mood import erase, feedback


def appraise(core, number, text, reason="OWNER_AFFECTION"):
    """A retained owner source and settled extraction, through the real record boundary."""
    core.settings.mood_enabled = True
    key = ingest(core, number, text)["event_id"]
    request_id = f"appraisal_{number}"
    core.store.execute(
        "INSERT INTO model_usage (request_id,scenario,provider,model,state,input_tokens,"
        "output_tokens,occurred_at) VALUES (?, 'memory_extract','synthetic','synthetic',"
        "'SETTLED',100,20,?)", (request_id, core.clock()),
    )
    signals = mood_appraisals.validate(core.store, [
        InteractionSignal(event_id=key, reason=reason, quote=text),
    ], {key})
    with core.store.transaction():
        mood_appraisals.record(core.store, core.settings, signals, request_id, core.clock())
    return key


def test_reasons_have_small_pad_deltas():
    """Five closed reason types with bounded PAD increments."""
    assert len(mood_appraisals.REASONS) == 5
    for reason, (p, a, d) in mood_appraisals.REASONS.items():
        assert reason in {
            "OWNER_AFFECTION",
            "OWNER_HURT_BY_REPLY",
            "OWNER_DISTRESS",
            "OWNER_EXPECTATION",
            "OWNER_PLAYFUL",
        }
        assert abs(p) <= 0.05 and abs(a) <= 0.03 and abs(d) <= 0.03


def test_quote_digest_uses_sha():
    """Quote digest for deduplication."""
    from companion.paid_budget import sha

    assert mood_appraisals.quote_digest("test") == sha("test")
    assert mood_appraisals.quote_digest(" trim ") == sha("trim")


def test_owner_text_extracts_only_plain_owner_messages(core):
    """Only plain private owner text, no commands or rich content."""
    ingest(core, 1, "owner words")
    assert mood_appraisals.owner_text(core.store, "evt_test_1") == "owner words"

    ingest(core, 2, "")
    core.store.execute(
        "UPDATE events SET actor_id='companion' WHERE event_id=?", ("evt_test_2",)
    )
    assert mood_appraisals.owner_text(core.store, "evt_test_2") is None

    assert mood_appraisals.owner_text(core.store, "evt_nonexistent") is None


def test_validate_filters_quote_not_in_owner_text(core):
    """Validation requires verbatim quote from owner's actual words."""
    ingest(core, 1, "actual text")
    signals = [
        InteractionSignal(
            event_id="evt_test_1", reason="OWNER_AFFECTION", quote="wrong text"
        ),
    ]
    kept = mood_appraisals.validate(core.store, signals, ["evt_test_1"])
    assert kept == []


def test_validate_filters_short_quotes(core):
    """Quotes must be at least 2 characters - validated at both model and logic level."""
    ingest(core, 1, "ab")
    # Test that validate() also filters quotes shorter than 2 chars in owner text
    # but quote field already enforces min_length=2 at Pydantic level
    signals = [
        InteractionSignal(event_id="evt_test_1", reason="OWNER_AFFECTION", quote="ab"),
    ]
    kept = mood_appraisals.validate(core.store, signals, ["evt_test_1"])
    assert len(kept) == 1  # Valid 2-char quote passes


def test_validate_requires_hurt_reply_within_one_hour_window(core):
    """OWNER_HURT_BY_REPLY needs recent delivered reply."""
    feedback(core, 50, text="base")
    core.clock.advance(3601)
    ingest(core, 150, "you hurt me")
    signals = [
        InteractionSignal(
            event_id="evt_test_150", reason="OWNER_HURT_BY_REPLY", quote="you hurt me"
        ),
    ]
    kept = mood_appraisals.validate(core.store, signals, ["evt_test_150"])
    assert kept == []

    # Within window works
    event_id = feedback(core, 200, text="your reply hurt")
    signals2 = [
        InteractionSignal(
            event_id=event_id, reason="OWNER_HURT_BY_REPLY", quote="your reply hurt"
        ),
    ]
    kept2 = mood_appraisals.validate(core.store, signals2, [event_id])
    assert len(kept2) == 1


def test_validate_accepts_valid_appraisal(core):
    """Valid signal passes validation."""
    ingest(core, 1, "miss you")
    signals = [
        InteractionSignal(event_id="evt_test_1", reason="OWNER_AFFECTION", quote="miss you"),
    ]
    kept = mood_appraisals.validate(core.store, signals, ["evt_test_1"])
    assert len(kept) == 1
    assert kept[0]["event_id"] == "evt_test_1"
    assert kept[0]["reason"] == "OWNER_AFFECTION"


def test_validate_deduplicates_by_event_id(core):
    """At most one signal per event."""
    ingest(core, 1, "joke and tease")
    signals = [
        InteractionSignal(event_id="evt_test_1", reason="OWNER_PLAYFUL", quote="joke"),
        InteractionSignal(event_id="evt_test_1", reason="OWNER_AFFECTION", quote="tease"),
    ]
    kept = mood_appraisals.validate(core.store, signals, ["evt_test_1"])
    assert len(kept) == 1


def test_record_writes_to_mood_appraisals_table(core):
    """Record writes validated signals to database."""
    core.settings.mood_enabled = True
    ingest(core, 1, "just kidding")
    signals = [
        InteractionSignal(
            event_id="evt_test_1", reason="OWNER_PLAYFUL", quote="just kidding"
        ),
    ]
    validated = mood_appraisals.validate(core.store, signals, ["evt_test_1"])

    # Create model_usage record for FK constraint
    core.store.execute(
        "INSERT INTO model_usage (request_id,scenario,provider,model,state,input_tokens,"
        "output_tokens,occurred_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("test_request", "memory_extract", "synthetic", "synthetic", "SETTLED",
         100, 20, core.clock()),
    )

    with core.store.transaction():
        mood_appraisals.record(
            core.store, core.settings, validated, "test_request", core.clock()
        )

    row = core.store.one(
        "SELECT event_id, reason FROM mood_appraisals WHERE event_id=?", ("evt_test_1",)
    )
    assert row["event_id"] == "evt_test_1"
    assert row["reason"] == "OWNER_PLAYFUL"


def test_record_respects_mood_enabled_flag(core):
    """Nothing recorded when mood_enabled is false."""
    core.settings.mood_enabled = False
    ingest(core, 1, "test")
    signals = [
        InteractionSignal(event_id="evt_test_1", reason="OWNER_AFFECTION", quote="test"),
    ]
    validated = mood_appraisals.validate(core.store, signals, ["evt_test_1"])

    with core.store.transaction():
        mood_appraisals.record(
            core.store, core.settings, validated, "test_request", core.clock()
        )

    count = core.store.one("SELECT COUNT(*) n FROM mood_appraisals")["n"]
    assert count == 0


def test_erase_removes_appraisals_by_event_ids(core):
    """Erasure cleanup removes appraisals."""
    core.settings.mood_enabled = True
    ingest(core, 1, "first")
    ingest(core, 2, "second")

    # Create model_usage record
    core.store.execute(
        "INSERT INTO model_usage (request_id,scenario,provider,model,state,input_tokens,"
        "output_tokens,occurred_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("test_request", "memory_extract", "synthetic", "synthetic", "SETTLED",
         100, 20, core.clock()),
    )

    for event_id, quote in [("evt_test_1", "first"), ("evt_test_2", "second")]:
        signals = [
            InteractionSignal(event_id=event_id, reason="OWNER_AFFECTION", quote=quote),
        ]
        validated = mood_appraisals.validate(core.store, signals, [event_id])
        with core.store.transaction():
            mood_appraisals.record(
                core.store, core.settings, validated, "test_request", core.clock()
            )

    assert core.store.one("SELECT COUNT(*) n FROM mood_appraisals")["n"] == 2

    with core.store.transaction():
        mood_appraisals.erase(core.store, ["evt_test_1"])

    assert core.store.one("SELECT COUNT(*) n FROM mood_appraisals")["n"] == 1
    remaining = core.store.one("SELECT event_id FROM mood_appraisals")
    assert remaining["event_id"] == "evt_test_2"


def test_still_valid_checks_quote_and_source_retention(core):
    """still_valid re-validates on every read."""
    core.settings.mood_enabled = True
    ingest(core, 1, "valid source")
    signals = [
        InteractionSignal(
            event_id="evt_test_1", reason="OWNER_AFFECTION", quote="valid source"
        ),
    ]
    validated = mood_appraisals.validate(core.store, signals, ["evt_test_1"])

    # Create model_usage record
    core.store.execute(
        "INSERT INTO model_usage (request_id,scenario,provider,model,state,input_tokens,"
        "output_tokens,occurred_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("test_request", "memory_extract", "synthetic", "synthetic", "SETTLED",
         100, 20, core.clock()),
    )

    with core.store.transaction():
        mood_appraisals.record(
            core.store, core.settings, validated, "test_request", core.clock()
        )

    row = core.store.one("SELECT * FROM mood_appraisals WHERE event_id=?", ("evt_test_1",))
    assert mood_appraisals.still_valid(core.store, row)

    # Erase source event
    erase(core, ["evt_test_1"])
    assert not mood_appraisals.still_valid(core.store, row)


def test_capped_enforces_daily_limit_per_reason(core):
    """At most DAILY_CAP per reason per day."""
    core.settings.mood_enabled = True
    core.clock.now = core.clock.now.replace(hour=12, minute=0, second=0, microsecond=0)

    # Create model_usage record
    core.store.execute(
        "INSERT INTO model_usage (request_id,scenario,provider,model,state,input_tokens,"
        "output_tokens,occurred_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("test_request", "memory_extract", "synthetic", "synthetic", "SETTLED",
         100, 20, core.clock()),
    )

    for i in range(5):
        ingest(core, i + 1, f"text{i}")
        signals = [
            InteractionSignal(
                event_id=f"evt_test_{i + 1}", reason="OWNER_AFFECTION", quote=f"text{i}"
            ),
        ]
        validated = mood_appraisals.validate(core.store, signals, [f"evt_test_{i + 1}"])
        with core.store.transaction():
            mood_appraisals.record(
                core.store, core.settings, validated, "test_request", core.clock()
            )
        core.clock.advance(60)

    rows = list(core.store.rows("SELECT * FROM mood_appraisals ORDER BY created_at"))
    assert len(rows) == 5

    # Raw rows stay; the projection keeps the earliest DAILY_CAP valid signals.
    kept = [s["event_id"] for s in core.mood.snapshot()["signals"]]
    assert kept == ["evt_test_1", "evt_test_2"]

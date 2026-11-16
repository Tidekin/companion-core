"""Per-reason immediate decay is a pure, deterministic fold over impulses."""

import pytest

from companion import mood

AT = "2026-11-16T00:00:00.000000+00:00"


def impulse(reason, at=AT, number=1, delta=(.04, 0.0, 0.0)):
    return {"at": at, "reason": reason, "delta": list(delta), "independent_id": f"id-{number}"}


def later(hours):
    return f"2026-11-16T{hours:02d}:00:00.000000+00:00"


@pytest.mark.parametrize(("reason", "hours"), [
    ("SHARED_IDEA_INTEREST", 3), ("OWNER_PLAYFUL", 1.5), ("OWNER_HURT_BY_REPLY", 8),
    ("COMMITMENT_MISSED", 12),
])
def test_each_reason_halves_over_its_own_half_life(reason, hours):
    assert mood.half_life(reason) == hours * 3600
    at = f"2026-11-16T{int(hours):02d}:{int(hours % 1 * 60):02d}:00.000000+00:00"
    assert mood.fold([impulse(reason)], at)["immediate"]["pleasure"] == pytest.approx(.02)


def test_unlisted_reason_keeps_the_original_three_hour_rate():
    assert mood.half_life("HELPFUL_FEEDBACK") == mood.HALF_LIVES["immediate"]
    assert mood.half_life(None) == mood.HALF_LIVES["immediate"]


def test_mixed_reasons_relax_separately_and_saturate_on_their_sum():
    signals = [impulse("OWNER_PLAYFUL"), impulse("COMMITMENT_MISSED", number=2,
                                                  delta=(-.03, 0.0, 0.0))]
    second = -.03 * (1 - .04 / .35)
    state = mood.fold(signals, later(12))
    assert state["immediate"]["pleasure"] == pytest.approx(.04 * 2 ** -8 + second / 2, abs=1e-4)


def test_undertone_still_relaxes_over_three_days():
    signals = [impulse("OWNER_PLAYFUL", number=n) for n in range(1, 4)]
    first = mood.fold(signals, AT)["undertone"]["pleasure"]
    assert first > 0
    three_days = "2026-11-19T00:00:00.000000+00:00"
    assert mood.fold(signals, three_days)["undertone"]["pleasure"] == pytest.approx(
        first / 2, abs=1e-4)


def test_repeats_halve_within_the_reason_half_life_and_recover_after_it():
    playful = [impulse("OWNER_PLAYFUL", f"2026-11-16T00:{m:02d}:00.000000+00:00", m)
               for m in (0, 10, 20)]
    assert mood.weights(playful) == [1, .5, .25]
    spaced = [impulse("OWNER_PLAYFUL", AT, 1), impulse("OWNER_PLAYFUL", later(2), 2)]
    assert mood.weights(spaced) == [1, 1]
    mixed = [impulse("OWNER_PLAYFUL", AT, 1), impulse("OWNER_AFFECTION", AT, 2),
             impulse("OWNER_PLAYFUL", AT, 3)]
    assert mood.weights(mixed) == [1, 1, .5]
    assert mood.fold(playful[:2], playful[1]["at"])["immediate"]["pleasure"] < (
        mood.fold([playful[0], impulse("OWNER_AFFECTION", playful[1]["at"], 9)],
                  playful[1]["at"])["immediate"]["pleasure"])

"""Unit tests for the rule fault sequence pairing (pure matching logic).

The pairing is order-preserving and one-to-one: an effect fault's start must
be strictly later than its cause's start and within the positive max lag;
the chosen pairing first maximizes the pair count, then minimizes the total
lag, and remaining ties resolve stably by fault id.
"""

import pytest

from app.pairing import PairableFault, pair_faults


def F(fid, start, end=None):
    return PairableFault(id=fid, start=float(start), end=float(end if end is not None else start + 10))


def pairs_of(result):
    return [(p.cause_id, p.effect_id) for p in result.pairs]


# --------------------------------------------------------------------------- #
# basic allow/deny rules
# --------------------------------------------------------------------------- #


def test_basic_pair_and_lag():
    result = pair_faults([F(1, 100)], [F(2, 150)], 100)
    assert pairs_of(result) == [(1, 2)]
    assert result.pairs[0].lag_seconds == pytest.approx(50)
    assert result.pairs[0].cause_start == 100
    assert result.pairs[0].effect_start == 150
    assert result.pair_count == 1
    assert result.total_lag_seconds == pytest.approx(50)
    assert result.unmatched_cause == []
    assert result.unmatched_effect == []


def test_effect_must_start_strictly_later():
    # identical start instants cannot anchor a pair
    result = pair_faults([F(1, 100)], [F(2, 100)], 100)
    assert result.pair_count == 0
    assert [f.id for f in result.unmatched_cause] == [1]
    assert [f.id for f in result.unmatched_effect] == [2]


def test_max_lag_boundary_is_inclusive():
    # effect.start == cause.start + max_lag still pairs
    result = pair_faults([F(1, 100)], [F(2, 700)], 600)
    assert pairs_of(result) == [(1, 2)]
    assert result.pairs[0].lag_seconds == pytest.approx(600)
    # one second beyond the max lag does not
    result = pair_faults([F(1, 100)], [F(2, 701)], 600)
    assert result.pair_count == 0


def test_each_fault_pairs_at_most_once():
    # two causes, one effect: only one pair, and the tighter lag wins
    result = pair_faults([F(1, 100), F(2, 101)], [F(3, 102)], 10)
    assert pairs_of(result) == [(2, 3)]
    assert [f.id for f in result.unmatched_cause] == [1]
    # one cause, two effects: only one pair, the tighter lag wins
    result = pair_faults([F(1, 100)], [F(2, 105), F(3, 108)], 10)
    assert pairs_of(result) == [(1, 2)]
    assert [f.id for f in result.unmatched_effect] == [3]


# --------------------------------------------------------------------------- #
# optimization order: count first, then total lag
# --------------------------------------------------------------------------- #


def test_maximizes_pair_count_first():
    # pairing c1->e1 blocks c2; the optimum leaves c1 unmatched instead
    causes = [F(1, 100), F(2, 105)]
    effects = [F(3, 106), F(4, 107)]
    result = pair_faults(causes, effects, 10)
    assert pairs_of(result) == [(1, 3), (2, 4)]
    assert result.pair_count == 2


def test_count_beats_lag():
    # (c1,e1) has the smallest single lag but caps the total at one pair;
    # the optimum takes two pairs with a larger total lag
    causes = [F(1, 100), F(2, 101)]
    effects = [F(3, 102), F(4, 180)]
    result = pair_faults(causes, effects, 100)
    assert pairs_of(result) == [(1, 3), (2, 4)]
    assert result.total_lag_seconds == pytest.approx(2 + 79)


def test_minimizes_total_lag_among_max_count_pairings():
    causes = [F(1, 100), F(2, 200)]
    effects = [F(3, 150), F(4, 160), F(5, 250)]
    result = pair_faults(causes, effects, 100)
    # (c1,e1)+(c2,e3) = 50+50 beats (c1,e2)+(c2,e3) = 60+50
    assert pairs_of(result) == [(1, 3), (2, 5)]
    assert result.total_lag_seconds == pytest.approx(100)


def test_order_preserving_pairs_come_out_in_time_order():
    causes = [F(1, 100), F(2, 101)]
    effects = [F(3, 150), F(4, 151)]
    result = pair_faults(causes, effects, 100)
    # the crossing assignment (c1->e2, c2->e1) would have the same total lag
    # but is not order-preserving; the aligned pairing is returned
    assert pairs_of(result) == [(1, 3), (2, 4)]
    starts = [(p.cause_start, p.effect_start) for p in result.pairs]
    assert starts == sorted(starts)


# --------------------------------------------------------------------------- #
# stable tie-break by fault id
# --------------------------------------------------------------------------- #


def test_tie_breaks_by_fault_id_on_effects():
    # two effects with the same start (same lag): the smaller fault id wins
    result = pair_faults([F(1, 100)], [F(9, 110), F(3, 110)], 20)
    assert pairs_of(result) == [(1, 3)]
    assert [f.id for f in result.unmatched_effect] == [9]


def test_tie_breaks_by_fault_id_on_causes():
    # two causes with the same start, one effect: the smaller fault id pairs
    result = pair_faults([F(7, 100), F(2, 100)], [F(1, 110)], 20)
    assert pairs_of(result) == [(2, 1)]
    assert [f.id for f in result.unmatched_cause] == [7]


def test_unsorted_input_is_stabilized():
    # input order must not matter: faults are processed in (start, id) order
    causes = [F(5, 300), F(4, 100), F(6, 200)]
    effects = [F(8, 210), F(7, 110), F(9, 310)]
    result = pair_faults(causes, effects, 20)
    assert pairs_of(result) == [(4, 7), (6, 8), (5, 9)]
    assert [p.lag_seconds for p in result.pairs] == [10, 10, 10]


# --------------------------------------------------------------------------- #
# rates, unmatched lists and edge cases
# --------------------------------------------------------------------------- #


def test_rates_and_unmatched():
    causes = [F(1, 100), F(2, 1000)]
    effects = [F(3, 150), F(4, 400), F(5, 1200)]
    result = pair_faults(causes, effects, 300)
    assert pairs_of(result) == [(1, 3), (2, 5)]
    assert result.cause_count == 2 and result.effect_count == 3
    assert result.cause_pair_rate == pytest.approx(1.0)
    assert result.effect_pair_rate == pytest.approx(2 / 3)
    assert [f.id for f in result.unmatched_effect] == [4]
    assert result.unmatched_cause == []


def test_empty_inputs():
    result = pair_faults([], [F(1, 100)], 10)
    assert result.pair_count == 0
    assert result.cause_pair_rate == 0.0
    assert result.effect_pair_rate == 0.0
    assert [f.id for f in result.unmatched_effect] == [1]
    result = pair_faults([F(1, 100)], [], 10)
    assert result.pair_count == 0
    assert [f.id for f in result.unmatched_cause] == [1]
    result = pair_faults([], [], 10)
    assert result.pair_count == 0


def test_invalid_max_lag_rejected():
    for bad in (0, -5, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            pair_faults([F(1, 100)], [F(2, 110)], bad)


def test_result_payload_shape():
    result = pair_faults([F(1, 100)], [F(2, 150)], 100)
    payload = result.to_dict()
    assert payload["pair_count"] == 1
    assert payload["pairs"] == [
        {
            "cause_fault_id": 1,
            "effect_fault_id": 2,
            "cause_start": 100.0,
            "effect_start": 150.0,
            "lag_seconds": 50.0,
        }
    ]
    assert payload["unmatched_cause"] == []
    assert payload["unmatched_effect"] == []
    assert payload["cause_faults_in_range"] == 1
    assert payload["effect_faults_in_range"] == 1

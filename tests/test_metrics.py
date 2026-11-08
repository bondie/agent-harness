import itertools

import numpy as np
import pytest

from agent_harness.metrics import (
    Prediction,
    a12,
    at_reference,
    compare,
    coverage,
    random_guessing_mae,
    relative_width,
    share_at_reference,
    standardized_accuracy,
    summarize,
)


def brute_force_random_guessing(actuals: list[float]) -> float:
    cases = list(enumerate(actuals))
    pairs = [abs(x - y) for (i, x), (j, y) in itertools.product(cases, repeat=2) if i != j]
    return sum(pairs) / len(pairs)


@pytest.mark.parametrize("actuals", [[1, 2], [0.5, 1, 1, 4], [3, 1, 8, 2, 2, 5.5]])
def test_random_guessing_matches_the_pairwise_definition(actuals):
    assert random_guessing_mae(actuals) == pytest.approx(brute_force_random_guessing(actuals))


def test_random_guessing_needs_two_cases():
    with pytest.raises(ValueError, match="two cases"):
        random_guessing_mae([1.0])


def test_standardized_accuracy_bounds():
    actuals = [1.0, 2.0, 4.0]
    assert standardized_accuracy(0.0, actuals) == 1.0
    assert standardized_accuracy(random_guessing_mae(actuals), actuals) == pytest.approx(0.0)


def test_summary_of_known_predictions():
    predictions = [
        Prediction(actual=1.0, median=1.0, p25=0.5, p75=2.0),
        Prediction(actual=2.0, median=1.0, p25=0.5, p75=1.5),
        Prediction(actual=4.0, median=5.0, p25=3.0, p75=6.0),
        Prediction(actual=2.0, median=2.5),
    ]
    summary = summarize(predictions)

    assert summary.n == 4
    assert summary.mae == pytest.approx((0 + 1 + 1 + 0.5) / 4)
    assert summary.mdae == pytest.approx(0.75)
    assert summary.mdmre == pytest.approx(np.median([0, 0.5, 0.25, 0.25]))
    assert summary.pred25 == pytest.approx(0.75)
    assert summary.coverage == pytest.approx(2 / 3)
    assert summary.relative_width == pytest.approx(np.median([1.5, 1.0, 0.6]))
    assert summary.mae_low <= summary.mae <= summary.mae_high


def test_point_only_estimates_have_no_range_metrics():
    summary = summarize([Prediction(1.0, 2.0), Prediction(3.0, 2.0)])
    assert summary.coverage is None
    assert summary.relative_width is None


def test_summary_needs_predictions():
    with pytest.raises(ValueError, match="Nothing"):
        summarize([])


def test_coverage_and_width_count_the_bounds_as_inside():
    edge = [Prediction(actual=2.0, median=2.0, p25=2.0, p75=2.0)]
    assert coverage(edge) == 1.0
    assert relative_width([Prediction(2.0, 2.0, 1.0, 3.0)]) == 1.0


def test_a12():
    assert a12(np.array([1.0, 1.0]), np.array([2.0, 2.0])) == 1.0
    assert a12(np.array([2.0]), np.array([1.0])) == 0.0
    assert a12(np.array([1.0, 2.0]), np.array([1.0, 2.0])) == 0.5


def test_compare_detects_a_clearly_better_estimator():
    rng = np.random.default_rng(1)
    actuals = rng.uniform(1, 10, size=40)
    close = [Prediction(a, a + 0.1) for a in actuals]
    far = [Prediction(a, a + 3.0) for a in actuals]

    result = compare(close, far)

    assert result.p_value < 0.001
    assert result.a12 == 1.0


def test_compare_identical_estimators():
    same = [Prediction(1.0, 2.0), Prediction(2.0, 2.0)]
    assert compare(same, same).p_value == 1.0


def test_compare_needs_the_same_tickets():
    with pytest.raises(ValueError, match="same tickets"):
        compare([Prediction(1.0, 1.0)], [])


def nearest_half_hour(hours: float) -> float:
    grid = [step / 2 for step in range(1, 400)]
    return min(grid, key=lambda candidate: (abs(candidate - hours), -candidate))


def test_share_at_reference_matches_the_definition():
    rng = np.random.default_rng(3)
    estimates = rng.integers(0, 24, size=60) / 4
    medians = rng.integers(0, 24, size=60) / 4 + rng.choice([0, 0.01, -0.01], size=60)
    pairs = [(float(e), float(m)) for e, m in zip(estimates, medians, strict=True)]
    expected = sum(nearest_half_hour(e) == nearest_half_hour(m) for e, m in pairs) / len(pairs)

    assert share_at_reference(pairs) == pytest.approx(expected)


def test_at_reference_rounds_both_sides():
    assert at_reference(1.25, 1.5)
    assert at_reference(1.2, 1.0)
    assert not at_reference(1.25, 1.2)
    assert at_reference(0.1, 0.4)


def test_share_at_reference_needs_pairs():
    with pytest.raises(ValueError, match="Nothing"):
        share_at_reference([])

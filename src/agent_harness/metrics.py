"""Accuracy of effort estimates, following the software effort estimation literature.

Point accuracy uses absolute errors in hours (MAE, MdAE) and standardized accuracy against
random guessing. MMRE is left out on purpose: it rewards underestimates (Foss et al. 2003).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy import stats

Floats = NDArray[np.float64]


@dataclass(frozen=True)
class Prediction:
    actual: float
    median: float
    p25: float | None = None
    p75: float | None = None


@dataclass(frozen=True)
class Summary:
    n: int
    mae: float
    mae_low: float
    mae_high: float
    mdae: float
    sa: float
    mdmre: float
    pred25: float
    coverage: float | None
    relative_width: float | None


def absolute_errors(predictions: Sequence[Prediction]) -> Floats:
    return np.array([abs(p.median - p.actual) for p in predictions], dtype=np.float64)


def random_guessing_mae(actuals: Sequence[float]) -> float:
    """Expected MAE of predicting each case with the actual value of another, random case.

    Exact form of Langdon et al. (2016): the mean absolute difference over all pairs.
    """
    values = np.sort(np.asarray(actuals, dtype=np.float64))
    n = len(values)
    if n < 2:
        raise ValueError("Random guessing needs at least two cases")
    weights = 2 * np.arange(n) - n + 1
    pair_sum = float(np.dot(weights, values))
    return 2 * pair_sum / (n * (n - 1))


def standardized_accuracy(mae: float, actuals: Sequence[float]) -> float:
    """1 means perfect, 0 means no better than random guessing, negative means worse."""
    return 1 - mae / random_guessing_mae(actuals)


def bootstrap_interval(
    errors: Floats, resamples: int = 2000, seed: int = 7, level: float = 0.95
) -> tuple[float, float]:
    """Percentile bootstrap of the mean absolute error."""
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, len(errors), size=(resamples, len(errors)))
    means = errors[picks].mean(axis=1)
    tail = (1 - level) / 2 * 100
    low, high = np.percentile(means, [tail, 100 - tail])
    return float(low), float(high)


def summarize(predictions: Sequence[Prediction]) -> Summary:
    if not predictions:
        raise ValueError("Nothing to summarize")
    errors = absolute_errors(predictions)
    actuals = np.array([p.actual for p in predictions], dtype=np.float64)
    relative = errors / actuals
    mae = float(errors.mean())
    low, high = bootstrap_interval(errors)
    ranged = [p for p in predictions if p.p25 is not None and p.p75 is not None]
    return Summary(
        n=len(predictions),
        mae=mae,
        mae_low=low,
        mae_high=high,
        mdae=float(np.median(errors)),
        sa=standardized_accuracy(mae, actuals.tolist()),
        mdmre=float(np.median(relative)),
        pred25=float(np.mean(relative <= 0.25)),
        coverage=coverage(ranged) if ranged else None,
        relative_width=relative_width(ranged) if ranged else None,
    )


def coverage(predictions: Sequence[Prediction]) -> float:
    """Share of actual values inside the predicted P25-P75 range; 0.5 is calibrated."""
    inside = [
        p.p25 <= p.actual <= p.p75 for p in predictions if p.p25 is not None and p.p75 is not None
    ]
    return float(np.mean(inside))


def relative_width(predictions: Sequence[Prediction]) -> float:
    """Median of (P75 - P25) / median: how wide the range is compared with the estimate."""
    widths = [
        (p.p75 - p.p25) / p.median
        for p in predictions
        if p.p25 is not None and p.p75 is not None and p.median > 0
    ]
    return float(np.median(widths))


@dataclass(frozen=True)
class Comparison:
    p_value: float
    a12: float
    """Probability that the first estimator's error is smaller than the second's."""


def compare(first: Sequence[Prediction], second: Sequence[Prediction]) -> Comparison:
    """Wilcoxon signed-rank test on paired absolute errors, with Vargha-Delaney A12."""
    if len(first) != len(second):
        raise ValueError("Both estimators must predict the same tickets")
    a, b = absolute_errors(first), absolute_errors(second)
    if np.allclose(a, b):
        return Comparison(p_value=1.0, a12=0.5)
    return Comparison(p_value=float(stats.wilcoxon(a, b).pvalue), a12=a12(a, b))


def a12(a: Floats, b: Floats) -> float:
    smaller = (a[:, None] < b[None, :]).sum()
    ties = (a[:, None] == b[None, :]).sum()
    return float((smaller + 0.5 * ties) / (len(a) * len(b)))

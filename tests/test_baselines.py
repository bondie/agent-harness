import pytest

from agent_harness.baselines import EraScaledMedian, Estimate, HumanEstimate, NeighbourMedian
from agent_harness.dataset import finished_hours, load_targets, sample


def targets_by_key(store, created_from="2025-01-01", created_to="2027-01-01"):
    return {t.key: t for t in load_targets(store, created_from, created_to)}


def test_targets_are_finished_tickets_with_time_under_the_cap(store):
    targets = targets_by_key(store)

    assert sorted(targets) == ["OPS-1", "SHOP-1", "SHOP-2", "SHOP-3", "SHOP-4", "SHOP-5", "SHOP-6"]
    assert targets["SHOP-5"].actual_hours == 2
    assert targets["SHOP-5"].human_hours == 1.5
    assert targets["SHOP-3"].human_hours is None


def test_sample_is_reproducible_and_ordered(store):
    targets = load_targets(store, "2025-01-01", "2027-01-01")

    picked = sample(targets, 3, seed=1)

    assert picked == sample(targets, 3, seed=1)
    assert [t.created for t in picked] == sorted(t.created for t in picked)
    assert sample(targets, None) == targets
    assert sample(targets, 99) == targets


def test_finished_hours_respects_the_cut_off_and_era(store):
    assert finished_hours(store, "2025-04-01T00:00:00+00:00") == [4, 6]
    assert finished_hours(store, "2026-02-02T00:00:00+00:00", created_from="2026-01-01") == [1]


def test_neighbour_median_uses_only_earlier_finished_tickets(store, searcher):
    estimate = NeighbourMedian(searcher, k=10).estimate(targets_by_key(store)["SHOP-6"])

    assert estimate is not None
    assert set(estimate.cited) == {"SHOP-1", "SHOP-2", "SHOP-3", "SHOP-4", "SHOP-5", "OPS-1"}
    assert estimate.median == pytest.approx(2.0)


def test_recent_median_ignores_tickets_before_the_change(store, searcher):
    recent = NeighbourMedian(searcher, since="2026-01-01", name="recent-median")

    estimate = recent.estimate(targets_by_key(store)["SHOP-6"])

    assert estimate is not None
    assert set(estimate.cited) == {"SHOP-4", "SHOP-5", "OPS-1"}


def test_no_estimate_without_earlier_finished_tickets(store, searcher):
    assert NeighbourMedian(searcher).estimate(targets_by_key(store)["SHOP-1"]) is None


def test_era_scaling_needs_enough_recent_tickets(store, searcher):
    era = EraScaledMedian(searcher, change="2026-01-01")

    estimate = era.estimate(targets_by_key(store)["SHOP-6"])

    assert estimate is not None
    assert estimate.note == "factor 1.00"
    assert era.factor("2026-03-01T00:00:00+00:00") == 1.0


def test_era_scaling_shrinks_older_tickets(store, searcher, monkeypatch):
    monkeypatch.setattr("agent_harness.baselines.MIN_RECENT", 1)
    era = EraScaledMedian(searcher, change="2026-01-01")

    estimate = era.estimate(targets_by_key(store)["SHOP-6"])

    factor = 1.0 / 4.0
    assert era.factor("2026-03-01T12:00:00+00:00") == pytest.approx(factor)
    assert estimate is not None
    assert estimate.note == f"factor {factor:.2f}"
    assert estimate.median == pytest.approx(1.0)


def test_era_scaling_falls_back_like_the_median(store, searcher):
    assert EraScaledMedian(searcher).estimate(targets_by_key(store)["SHOP-1"]) is None


def test_human_estimate(store):
    targets = targets_by_key(store)
    assert HumanEstimate().estimate(targets["SHOP-5"]) == Estimate(median=1.5)
    assert HumanEstimate().estimate(targets["SHOP-3"]) is None

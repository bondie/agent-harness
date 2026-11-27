import json
import math
import random
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_harness import cli, conformal
from agent_harness.conformal import (
    ABOVE,
    BELOW,
    FLOOR_HOURS,
    INSIDE,
    Calibrated,
    Calibration,
    Evaluation,
    Outcome,
    Pair,
)
from agent_harness.report import report
from agent_harness.runner import Record

START = datetime(2026, 1, 1, tzinfo=UTC)


def stamp(hours: float) -> str:
    return (START + timedelta(hours=hours)).isoformat(timespec="seconds")


def pair(
    number: int,
    created: float,
    finished: float | None,
    actual: float = 1.0,
    p25: float = 0.5,
    median: float = 1.0,
    p75: float = 2.0,
) -> Pair:
    return Pair(
        id=f"src/T-{number}",
        created=stamp(created),
        finished=None if finished is None else stamp(finished),
        actual=actual,
        median=median,
        p25=p25,
        p75=p75,
    )


def shifted(count: int, seed: int = 3, scale: float = 2.0, spread: float = 0.6) -> list[Pair]:
    """Intervals placed `scale` times too high around lognormal actual times, one a day."""
    rng = random.Random(seed)  # noqa: S311
    pairs = []
    for number in range(count):
        median = rng.uniform(0.5, 4.0)
        actual = median / scale * math.exp(rng.gauss(0, spread))
        pairs.append(
            pair(
                number,
                created=24 * number,
                finished=24 * number + 12,
                actual=actual,
                p25=median * 0.7,
                median=median,
                p75=median * 1.4,
            )
        )
    return pairs


def calibration(**changes) -> Calibration:
    return replace(Calibration(1.0, 1.0, 50, 0.5, "", ""), **changes)


def brute_quantile(scores: list[float], level: float) -> float:
    """The smallest score with at least ceil(level * (n + 1)) scores at or below it."""
    need = math.ceil(level * (len(scores) + 1) - 1e-9)
    return min(score for score in scores if sum(other <= score for other in scores) >= need)


@given(
    st.lists(
        st.floats(min_value=-5, max_value=5, allow_nan=False).map(lambda x: round(x, 1)),
        min_size=9,
        max_size=60,
    ),
    st.sampled_from([0.5, 0.75, 0.9]),
)
def test_conformal_quantile_matches_the_brute_force_definition(scores, level):
    assert conformal.conformal_quantile(scores, level) == brute_quantile(scores, level)


def test_needed_is_the_fewest_scores_with_a_finite_quantile():
    assert conformal.needed(0.75) == 3
    assert conformal.needed(0.9) == 9
    assert conformal.rank(0.9, 9) == 9
    with pytest.raises(ValueError, match="at least 9"):
        conformal.conformal_quantile([0.0] * 8, 0.9)


def test_fit_rejects_nothing_and_a_coverage_outside_zero_and_one():
    with pytest.raises(ValueError, match="Nothing"):
        conformal.fit([])
    with pytest.raises(ValueError, match="between 0 and 1"):
        conformal.fit(shifted(10), coverage=1.0)


def test_fit_recovers_the_shift_of_a_known_distribution():
    fitted = conformal.fit(shifted(4000))
    # actual = median / 2 * exp(N(0, 0.6)); the 25% and 75% quantiles are median / 2 * exp(∓0.405)
    assert fitted.lower_factor == pytest.approx(0.5 * math.exp(-0.6 * 0.6745) / 0.7, rel=0.05)
    assert fitted.upper_factor == pytest.approx(0.5 * math.exp(0.6 * 0.6745) / 1.4, rel=0.05)
    assert fitted.p90_factor == pytest.approx(0.5 * math.exp(0.6 * 1.2816) / 1.4, rel=0.05)
    assert fitted.n == 4000
    assert fitted.finished_from == stamp(12)
    assert fitted.finished_to == stamp(24 * 3999 + 12)


def test_fit_leaves_out_p90_below_its_minimum():
    assert conformal.fit(shifted(49), p90_minimum=50).p90_factor is None
    assert conformal.fit(shifted(50), p90_minimum=50).p90_factor is not None
    assert conformal.fit(shifted(8)).p90_factor is None


def test_rolling_coverage_on_a_known_distribution_is_near_nominal():
    pairs = shifted(1500, scale=1.3)
    evaluation = conformal.rolling(pairs, 200)
    before, after = evaluation.shares(after=False), evaluation.shares(after=True)
    assert before.below > 0.4
    assert after.below == pytest.approx(0.25, abs=0.04)
    assert after.inside == pytest.approx(0.5, abs=0.04)
    assert after.above == pytest.approx(0.25, abs=0.04)
    found = evaluation.bound(after=True)
    assert found is not None
    assert found.above == pytest.approx(0.1, abs=0.03)
    assert conformal.p90_stable(evaluation)


def test_the_unchanged_median_caps_how_far_a_range_can_move_down():
    after = conformal.rolling(shifted(1500, scale=2.0), 200).shares(after=True)
    assert after.below == pytest.approx(0.25, abs=0.04)
    assert after.above < 0.2


def test_a_ticket_finished_after_the_target_was_created_is_never_used():
    calibrating = [pair(n, created=n, finished=10 + n, actual=1.0) for n in range(30)]
    late = pair(99, created=0, finished=200, actual=1000.0)
    on_the_dot = pair(98, created=0, finished=100, actual=1000.0)
    target = pair(100, created=100, finished=300, actual=1.0, p25=0.5, median=1.0, p75=2.0)
    evaluation = conformal.rolling([*calibrating, late, on_the_dot, target], None)
    scored = {outcome.id: outcome for outcome in evaluation.outcomes}
    expected = conformal.apply(0.5, 1.0, 2.0, conformal.fit(calibrating, p90_minimum=50))
    assert scored[target.id].after == expected


def test_the_window_keeps_the_latest_finished_tickets():
    old = [pair(n, created=n, finished=n + 1, actual=50.0) for n in range(40)]
    recent = [pair(100 + n, created=50 + n, finished=60 + n, actual=0.2) for n in range(50)]
    target = pair(500, created=1000, finished=1001)
    outcome = next(
        outcome
        for outcome in conformal.rolling([*old, *recent, target], 50).outcomes
        if outcome.id == target.id
    )
    assert outcome.after == conformal.apply(
        0.5, 1.0, 2.0, conformal.fit(recent, p90_minimum=conformal.P90_MINIMUM)
    )


def test_tickets_without_enough_calibration_are_skipped_the_same_in_every_window():
    pairs = shifted(120)
    evaluations = [conformal.rolling(pairs, window) for window in (50, 100, None)]
    assert {evaluation.skipped for evaluation in evaluations} == {30}
    assert {len(evaluation.outcomes) for evaluation in evaluations} == {90}
    with pytest.raises(ValueError, match="below the minimum"):
        conformal.rolling(pairs, 20)


def test_a_ticket_missing_from_the_index_is_scored_but_never_calibrates():
    pairs = shifted(60)
    unknown = pair(999, created=0, finished=None, actual=1000.0)
    evaluation = conformal.rolling([*pairs, unknown], None)
    reference = conformal.rolling(pairs, None)
    assert evaluation.skipped == reference.skipped + 1
    assert [o.after for o in evaluation.outcomes] == [o.after for o in reference.outcomes]


def test_rolling_can_calibrate_on_another_estimators_tickets():
    other = shifted(80, seed=5)
    target = pair(1000, created=24 * 100, finished=24 * 101)
    evaluation = conformal.rolling([target], None, fit_on=other)
    assert evaluation.outcomes[0].after == conformal.apply(
        0.5, 1.0, 2.0, conformal.fit(other, p90_minimum=conformal.P90_MINIMUM)
    )
    assert evaluation.latest == conformal.fit(other, p90_minimum=conformal.P90_MINIMUM)


def test_apply_keeps_the_median_inside_and_p90_above_p75():
    raised = conformal.apply(0.5, 1.0, 2.0, calibration(lower_factor=3.0, upper_factor=0.2))
    assert raised == Calibrated(1.0, 1.0, None)
    with_p90 = conformal.apply(
        0.5, 1.0, 2.0, calibration(upper_factor=2.0, p90_factor=1.1, p90_base="p75")
    )
    assert with_p90 == Calibrated(0.5, 4.0, 4.0)
    from_median = conformal.apply(0.5, 1.0, 2.0, calibration(p90_factor=3.0, p90_base="median"))
    assert from_median.p90 == 3.0


def test_a_non_positive_p25_is_floored():
    scaled = conformal.apply(0.0, 1.0, 2.0, calibration(lower_factor=2.0))
    assert scaled.p25 == pytest.approx(FLOOR_HOURS * 2)
    assert conformal.lower_score(pair(1, 0, 1, actual=FLOOR_HOURS, p25=-1.0)) == 0.0


def test_an_actual_on_the_calibrated_boundary_is_inside():
    pairs = [pair(n, created=n, finished=n + 1, actual=0.3 + n / 100) for n in range(30)]
    fitted = conformal.fit(pairs)
    q_lo = -math.log(fitted.lower_factor)
    boundary = math.exp(math.log(0.5) - q_lo)
    target = pair(100, created=500, finished=600, actual=boundary)
    outcome = conformal.rolling([*pairs, target], None).outcomes[-1]
    assert outcome.id == target.id
    assert conformal.position(target.actual, outcome.after.p25, outcome.after.p75) == INSIDE
    assert conformal.position(0.1, 0.5, 2.0) == BELOW
    assert conformal.position(2.5, 0.5, 2.0) == ABOVE
    assert conformal.position(2.0 * (1 + 1e-12), 0.5, 2.0) == INSIDE


def outcome(number: int, actual: float, p25: float, p75: float, p90: float | None) -> Outcome:
    return Outcome(
        id=f"src/T-{number}",
        created=stamp(number),
        actual=actual,
        median=1.0,
        before=Calibrated(0.5, 2.0, 2.0),
        after=Calibrated(p25, p75, p90),
    )


def evaluation(window: int | None, outcomes: list[Outcome]) -> Evaluation:
    return Evaluation(window, 0.5, "p75", tuple(outcomes), 0, None)


def test_recommend_takes_the_closest_coverage_then_the_narrower_range():
    wide = evaluation(50, [outcome(n, 1.0 if n % 2 else 9.0, 0.5, 3.0, None) for n in range(100)])
    narrow = evaluation(
        100, [outcome(n, 1.0 if n % 2 else 9.0, 0.8, 2.0, None) for n in range(100)]
    )
    off = evaluation(200, [outcome(n, 1.0, 0.5, 2.0, None) for n in range(100)])
    assert conformal.recommend([wide, narrow, off]) is narrow


def test_p90_is_stable_only_when_both_halves_are_near_ten_percent():
    even = [outcome(n, 9.0 if n % 10 == 0 else 1.0, 0.5, 2.0, 4.0) for n in range(200)]
    drifting = [outcome(n, 9.0 if n < 20 else 1.0, 0.5, 2.0, 4.0) for n in range(200)]
    assert conformal.p90_stable(evaluation(100, even))
    assert not conformal.p90_stable(evaluation(50, drifting))
    assert not conformal.p90_stable(evaluation(None, [outcome(1, 1.0, 0.5, 2.0, None)]))
    found = conformal.smallest_stable(
        [evaluation(None, even), evaluation(50, drifting), evaluation(100, even)]
    )
    assert found is not None
    assert found.window == 100
    assert conformal.smallest_stable([evaluation(50, drifting)]) is None


def test_shares_and_bound_of_nothing():
    assert math.isnan(conformal.shares([], after=True).inside)
    assert conformal.bound([outcome(1, 1.0, 0.5, 2.0, None)], after=True) is None


def test_wilson_interval():
    low, high = conformal.wilson(0.5, 100)
    assert low == pytest.approx(0.404, abs=0.001)
    assert high == pytest.approx(0.596, abs=0.001)
    assert all(math.isnan(value) for value in conformal.wilson(0.5, 0))


def index(path=":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE tickets (source TEXT, key TEXT, time_spent_seconds INTEGER, "
        "last_logged TEXT, updated TEXT)"
    )
    return db


def test_finish_times_fall_back_to_updated_and_skip_tickets_without_time():
    db = index()
    db.executemany(
        "INSERT INTO tickets VALUES (?, ?, ?, ?, ?)",
        [
            ("src", "T-1", 3600, stamp(5), stamp(50)),
            ("src", "T-2", 3600, None, stamp(7)),
            ("src", "T-3", 0, stamp(1), stamp(2)),
            ("other", "T-1", 3600, stamp(9), stamp(9)),
        ],
    )
    finishes = conformal.finish_times(db, ["src/T-1", "src/T-2", "src/T-3", "src/T-4"])
    assert finishes.at == {"src/T-1": stamp(5), "src/T-2": stamp(7)}
    assert finishes.from_updated == frozenset({"src/T-2"})


def test_the_index_opens_read_only(tmp_path):
    path = tmp_path / "index.db"
    index(str(path)).close()
    db = conformal.open_index(path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.execute("INSERT INTO tickets VALUES ('a', 'b', 1, NULL, NULL)")
    db.close()


def record(number: int, created: float, actual: float, **changes) -> Record:
    base = Record(
        variant="recent-median",
        id=f"src/T-{number}",
        project="P",
        issue_type="Task",
        created=stamp(created),
        actual=actual,
        human=None,
        median=2.0,
        p25=1.4,
        p75=2.8,
    )
    return replace(base, **changes)


def test_prepare_keeps_only_usable_ranges():
    records = [
        record(1, 0, 1.0),
        record(2, 1, 1.0, error="no estimate", median=None, p25=None, p75=None),
        record(3, 2, 1.0, p25=None),
        record(4, 3, 1.0, p25=3.0),
        record(5, 4, 1.0),
    ]
    finishes = conformal.Finishes({"src/T-1": stamp(10)}, frozenset({"src/T-1"}))
    prepared = conformal.prepare(records, finishes)
    assert [p.id for p in prepared.pairs] == ["src/T-1", "src/T-5"]
    assert prepared.without_interval == 3
    assert prepared.without_finish == 1
    assert prepared.from_updated == 1


def test_default_path_is_the_estimators_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = tmp_path / ".claude" / "agent-memory" / "agent-harness-estimator"
    assert conformal.default_path() == expected / "interval-calibration.json"


@pytest.fixture
def fake_run(tmp_path):
    """A run where every estimate is twice the logged time, and an index of when it was logged."""
    rng = random.Random(11)  # noqa: S311
    run = tmp_path / "run"
    run.mkdir()
    db = index(str(tmp_path / "index.db"))
    lines = []
    for number in range(160):
        actual = math.exp(rng.gauss(0, 0.5))
        median = 2 * actual * math.exp(rng.gauss(0, 0.3))
        item = record(number, 24 * number, actual, median=median, p25=median / 2, p75=median * 2)
        lines.append(json.dumps(asdict(item)))
        db.execute(
            "INSERT INTO tickets VALUES (?, ?, ?, ?, ?)",
            ("src", f"T-{number}", 3600, stamp(24 * number + 20), stamp(24 * number + 30)),
        )
    db.commit()
    db.close()
    (run / "recent-median.jsonl").write_text("\n".join(lines) + "\n")
    config = tmp_path / "config.yaml"
    config.write_text(f"index_path: {tmp_path / 'index.db'}\n")
    return run, config


def test_cli_calibrate_prints_the_windows_and_writes_the_calibration(fake_run, tmp_path, capsys):
    run, config = fake_run
    target = tmp_path / "memory" / "interval-calibration.json"
    assert cli.main(["calibrate", str(run), "--config", str(config), "--write", str(target)]) == 0
    out = capsys.readouterr().out
    assert "160 tickets, 160 with a usable range, 0 of them not in the index" in out
    assert "| window 100 |" in out
    assert "| all earlier tickets |" in out
    assert "| P90 from P75 |" in out
    assert "Recommended: window" in out
    saved = json.loads(target.read_text())
    assert saved["variant"] == "recent-median"
    assert saved["coverage"] == 0.5
    assert saved["p90_base"] == "p75"
    assert saved["lower_factor"] < 1
    assert saved["n"] == (160 if saved["window"] == "all" else min(saved["window"], 160))
    assert saved["rolling"]["tickets"] + saved["rolling"]["skipped"] == 160
    assert saved["finished_to"] == stamp(24 * 159 + 20)
    assert (saved["p90_factor"] is None) == (saved["p90_refused"] is not None)


def test_cli_calibrate_on_a_jsonl_with_a_window_and_another_pool(fake_run, capsys):
    run, config = fake_run
    jsonl = run / "recent-median.jsonl"
    arguments = ["calibrate", str(jsonl), "--window", "60", "--fit-on", str(jsonl)]
    assert cli.main([*arguments, "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "| window 60 |" in out
    assert "Current calibration, window 60 (60 tickets" in out


def test_cli_calibrate_refuses_a_bad_window_and_a_missing_variant(fake_run, capsys):
    run, config = fake_run
    with pytest.raises(SystemExit):
        cli.main(["calibrate", str(run), "--window", "lots"])
    with pytest.raises(SystemExit):
        cli.main(["calibrate", str(run), "--window", "0"])
    assert cli.main(["calibrate", str(run), "--variant", "x", "--config", str(config)]) == 1
    assert "No x results" in capsys.readouterr().err


def test_cli_report_adds_the_calibrated_section(fake_run, capsys):
    run, config = fake_run
    arguments = ["report", str(run), "--baseline", "recent-median", "--conformal", "all"]
    assert cli.main([*arguments, "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "## Calibrated P25-P75 and P90 (rolling conformal, all earlier tickets)" in out
    row = next(line for line in out.splitlines() if line.startswith("| recent-median | 130 | 30"))
    assert row.endswith("|")


def test_cli_report_refuses_a_bad_window(fake_run, capsys):
    run, config = fake_run
    assert cli.main(["report", str(run), "--conformal", "soon", "--config", str(config)]) == 1
    assert "--conformal: expected a number of tickets or all" in capsys.readouterr().err


def test_report_without_calibration_has_no_section():
    records = [record(n, n, 1.0 + n) for n in range(3)]
    assert "Calibrated" not in report({"recent-median": records}, baseline="recent-median")

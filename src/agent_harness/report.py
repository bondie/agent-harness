"""Aggregate report of a backtest run: no ticket text, keys or project names."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence

from .llm import REJECTED, REJECTED_UNCITED, STRUCTURED
from .metrics import Prediction, Summary, compare, share_at_reference, summarize
from .runner import Record

HEADER = (
    "| Variant | n | MAE h [95% CI] | MdAE h | SA | MdMRE | PRED(25) | P25-P75 coverage "
    "| Relative width | vs baseline p | Â12 | Cost USD |\n"
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
)


MINUTE = 1 / 60
DRIFT = 0.005


def prediction(record: Record) -> Prediction:
    if record.median is None:
        raise ValueError(f"{record.variant} has no estimate for {record.id}")
    return Prediction(actual=record.actual, median=record.median, p25=record.p25, p75=record.p75)


def succeeded(records: Sequence[Record]) -> dict[str, Record]:
    return {record.id: record for record in records if not record.error}


def common_ids(runs: Mapping[str, dict[str, Record]]) -> list[str]:
    if not runs:
        return []
    shared = set.intersection(*(set(records) for records in runs.values()))
    return sorted(shared)


def table(
    runs: Mapping[str, dict[str, Record]],
    ids: Sequence[str],
    baseline: str,
) -> str:
    if len(ids) < 2:
        return "_Fewer than two tickets: nothing to compare._"
    rows = [HEADER]
    reference = [prediction(runs[baseline][i]) for i in ids] if baseline in runs else None
    for variant in sorted(runs, key=lambda name: (name != baseline, name)):
        records = runs[variant]
        predictions = [prediction(records[i]) for i in ids]
        summary = summarize(predictions)
        versus = "-"
        a12 = "-"
        if reference is not None and variant != baseline:
            result = compare(predictions, reference)
            versus = _p(result.p_value)
            a12 = f"{result.a12:.2f}"
        cost = sum(records[i].cost_usd for i in ids)
        rows.append(_row(variant, summary, versus, a12, cost))
    return "\n".join(rows)


def _row(variant: str, s: Summary, versus: str, a12: str, cost: float) -> str:
    return (
        f"| {variant} | {s.n} | {s.mae:.2f} [{s.mae_low:.2f}-{s.mae_high:.2f}] | {s.mdae:.2f} "
        f"| {s.sa:.2f} | {s.mdmre:.2f} | {s.pred25:.0%} | {_share(s.coverage)} "
        f"| {_number(s.relative_width)} | {versus} | {a12} | {cost:.2f} |"
    )


def _p(value: float) -> str:
    return "<0.001" if value < 0.001 else f"{value:.3f}"


def _share(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _number(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def project_labels(records: Sequence[Record]) -> dict[str, str]:
    """Projects renamed A, B, C… by size, so a public report names no client."""
    counts = Counter(record.project for record in records)
    ordered = sorted(counts, key=lambda project: (-counts[project], project))
    return {project: f"Project {chr(ord('A') + i)}" for i, project in enumerate(ordered)}


def report(
    runs: Mapping[str, Sequence[Record]],
    baseline: str = "neighbour-median",
    human: str = "human",
) -> str:
    ok = {variant: succeeded(records) for variant, records in runs.items()}
    models = {variant: records for variant, records in ok.items() if variant != human}
    ids = common_ids(models)
    sections = [
        "## All variants on the tickets every variant estimated",
        table(models, ids, baseline),
    ]
    if human in ok:
        with_human = [i for i in ids if i in ok[human]]
        sections += [
            "## With the human estimate",
            "The first original estimate, as it stood when time was first logged.",
            table({**models, human: ok[human]}, with_human, baseline),
        ]
        differs = [
            i for i in with_human if abs((ok[human][i].median or 0) - ok[human][i].actual) > MINUTE
        ]
        sections += [
            "## Tickets whose logged time differs from the human estimate",
            "Where time is logged to match the estimate, the human estimate is circular; "
            "this subset leaves those tickets out.",
            table({**models, human: ok[human]}, differs, baseline),
        ]
    sections += _by_project(models, ids, baseline)
    sections += [_notes(runs, ids)]
    return "\n\n".join(sections) + "\n"


def _by_project(
    models: Mapping[str, dict[str, Record]], ids: Sequence[str], baseline: str
) -> list[str]:
    if not models or not ids:
        return []
    first = next(iter(models.values()))
    labels = project_labels([first[i] for i in ids])
    sections = []
    for project, label in labels.items():
        subset = [i for i in ids if first[i].project == project]
        sections += [f"### {label}", table(models, subset, baseline)]
    return ["## By project", *sections]


def _notes(runs: Mapping[str, Sequence[Record]], ids: Sequence[str]) -> str:
    lines = ["## Run notes"]
    for variant, records in runs.items():
        failed = sum(1 for record in records if record.error)
        redacted = sum(1 for record in records if record.note == "redacted")
        seconds = _mean(records, lambda record: record.seconds)
        lines.append(
            f"- {variant}: {len(records)} tickets, {failed} without an estimate, "
            f"{redacted} with estimates masked in the text, "
            f"{seconds:.1f} s per ticket"
            + _anchoring(records)
            + _rejections(variant, records)
            + _memory(records)
        )
    lines.append(f"- Compared on {len(ids)} tickets estimated by every variant.")
    return "\n".join(lines)


def _anchoring(records: Sequence[Record]) -> str:
    pairs = [
        (record.median, record.reference_median)
        for record in records
        if record.median is not None and record.reference_median is not None
    ]
    if not pairs:
        return ""
    return (
        f"; {share_at_reference(pairs):.0%} of {len(pairs)} estimates at the reference median "
        "(both rounded to 0.5 h)"
    )


def _rejections(variant: str, records: Sequence[Record]) -> str:
    if STRUCTURED not in variant:
        return ""
    rejected = sum(1 for record in records if record.error.startswith((REJECTED, REJECTED_UNCITED)))
    uncited = sum(1 for record in records if record.error.startswith(REJECTED_UNCITED))
    retried = sum(1 for record in records if record.retried)
    kept = sum(1 for record in records if record.check_errors)
    return (
        f"; {rejected} rejected by the estimate check, {uncited} of them for an uncited adjustment"
        f"; {retried} retried after the check, {kept} kept after the check failed (stated total)"
        + _drift(records)
    )


def _drift(records: Sequence[Record]) -> str:
    """Reference-mode blocks whose median is not the one retrieved: the model moved the
    starting point without an adjustment."""
    pairs = [
        (record.block_reference_median, record.reference_median)
        for record in records
        if record.block_mode == "reference"
        and record.block_reference_median is not None
        and record.reference_median is not None
    ]
    if not pairs:
        return ""
    drifted = sum(1 for written, retrieved in pairs if abs(written - retrieved) > DRIFT)
    return (
        f"; median drift: {drifted} of {len(pairs)} reference-mode blocks "
        f"({drifted / len(pairs):.0%}) wrote a median other than the retrieved one"
    )


def _memory(records: Sequence[Record]) -> str:
    used = [record for record in records if record.lessons is not None]
    if not used:
        return ""
    lessons = [record.lessons or 0 for record in used]
    return (
        f"; memory: {sum(lessons) / len(lessons):.1f} lessons per ticket (at most {max(lessons)}) "
        f"of {used[0].lessons_dated} dated, {used[0].lessons_undated} without a date left out"
    )


def _mean(records: Sequence[Record], value: Callable[[Record], float]) -> float:
    return sum(value(record) for record in records) / len(records) if records else 0.0

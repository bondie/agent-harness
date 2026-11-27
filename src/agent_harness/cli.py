from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ticket_precedent.config import (
    ALL_PLATFORMS,
    Config,
    build_searcher,
    default_config_path,
    load_config,
)
from ticket_precedent.search import Searcher
from ticket_precedent.store import Store

from . import activetime, conformal, memory, prospective, team
from .artifacts import (
    DISPOSITIONS,
    SOURCES,
    append_escaped,
    harness_home,
    incident_case,
    one_line,
    pick_ticket,
    read_object,
    set_disposition,
    ticket_dir,
    ticket_dirs,
    write_json,
)
from .baselines import EraScaledMedian, Estimator, HumanEstimate, NeighbourMedian
from .dataset import load_targets, sample
from .estimate_check import estimate_step
from .gitlog import (
    DEFAULT_TEST_PATTERNS,
    FileChange,
    changes,
    changes_by_term,
    diff,
    resolve,
    ticket_of,
)
from .llm import STRUCTURED, WITH_MEMORY, Backend, ClaudeCode, LLMEstimator, Ollama
from .memory import Memory, load_memory
from .report import report, window_name, window_tables
from .runner import Record, claim_directory, load_records, load_run, run, run_file

BASELINES = ("neighbour-median", "recent-median", "era-scaled-median", "human")
LLM_CONTEXTS = ("llm-none", "llm-keyword", "llm-hybrid", "llm-hybrid-recent")
REV_HELP = "read only the commits reachable from this revision, e.g. main (default: all refs)"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.handler
    try:
        return handler(args)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness-eval",
        description="Backtest effort estimators on finished tickets, measure the team and lint "
        "the agents' memory.",
    )
    commands = parser.add_subparsers(required=True)

    cmd = commands.add_parser("run", help="estimate finished tickets, resuming a previous run")
    cmd.add_argument("--out", type=Path, required=True, help="run directory (kept private)")
    cmd.add_argument("--from", dest="created_from", required=True, help="YYYY-MM-DD")
    cmd.add_argument("--to", dest="created_to", required=True, help="YYYY-MM-DD, exclusive")
    cmd.add_argument(
        "--variants",
        default=",".join(BASELINES),
        help=f"comma-separated: {', '.join(BASELINES + LLM_CONTEXTS)}; an llm-* variant "
        f"takes {STRUCTURED} (code computes the number from the harness-estimate block) "
        f"and then {WITH_MEMORY} (lessons from --memory learned before the ticket)",
    )
    cmd.add_argument(
        "--backend",
        action="append",
        default=[],
        help="for llm-* variants: claude:MODEL or ollama:MODEL (repeatable)",
    )
    cmd.add_argument("--sample", type=int, help="random subset of this many tickets")
    cmd.add_argument("--seed", type=int, default=7)
    cmd.add_argument("--k", type=int, default=10, help="tickets in the reference class")
    cmd.add_argument("--max-hours", type=float, default=40.0)
    cmd.add_argument("--change", default="2026-01-01", help="date of a process change")
    cmd.add_argument("--process-note", help="what changed, told to the LLM variants")
    cmd.add_argument("--config", type=Path, help="ticket-precedent configuration file")
    cmd.add_argument(
        "--platform",
        help="take the tickets and their reference classes from this platform's sources "
        f"(default: the config's default_platform; {ALL_PLATFORMS}: every source)",
    )
    cmd.add_argument("--memory", type=Path, help="agent memory directory for +memory variants")
    cmd.set_defaults(handler=_run)

    cmd = commands.add_parser("report", help="aggregate Markdown report of a run")
    cmd.add_argument("directory", type=Path)
    cmd.add_argument("--baseline", default="neighbour-median")
    cmd.add_argument(
        "--conformal",
        metavar="WINDOW",
        help="add the rolling conformal calibration of each variant's range on this window "
        "(a number of tickets, or all); reads finish times from the index",
    )
    cmd.add_argument("--config", type=Path, help="ticket-precedent configuration file")
    cmd.set_defaults(handler=_report)

    cmd = commands.add_parser(
        "calibrate",
        help="conformal calibration of a variant's P25-P75 and P90, measured on a rolling window",
    )
    cmd.add_argument("run", type=Path, help="run directory, or one variant's .jsonl")
    cmd.add_argument("--variant", default="recent-median", help="in a run directory")
    cmd.add_argument(
        "--window",
        type=_window,
        help="tickets to calibrate on, or all (default: the window closest to nominal coverage)",
    )
    cmd.add_argument("--coverage", type=float, default=0.5, help="of the central range")
    cmd.add_argument(
        "--fit-on",
        type=Path,
        help="calibrate on another variant's .jsonl and apply its factors to this range",
    )
    cmd.add_argument("--config", type=Path, help="ticket-precedent configuration file")
    cmd.add_argument(
        "--write",
        type=Path,
        nargs="?",
        const=conformal.default_path(),
        metavar="PATH",
        help=f"save the current calibration (default: {conformal.default_path()})",
    )
    cmd.set_defaults(handler=_calibrate)

    group = commands.add_parser("memory", help="check the lessons in the agents' memory")
    tasks = group.add_subparsers(required=True)
    default_dir = memory.agent_memory_root()
    cmd = tasks.add_parser(
        "lint", help="undated, sourceless and expired lessons, dead paths, files over the cap"
    )
    cmd.add_argument("--dir", type=Path, default=default_dir, help="one directory per agent")
    cmd.set_defaults(handler=_memory_lint)
    cmd = tasks.add_parser("prune", help="remove expired lessons (a dry run without --apply)")
    cmd.add_argument("--dir", type=Path, default=default_dir, help="one directory per agent")
    cmd.add_argument("--expired", action="store_true", required=True)
    cmd.add_argument("--apply", action="store_true", help="rewrite the files")
    cmd.set_defaults(handler=_memory_prune)
    cmd = tasks.add_parser(
        "compact",
        help="move whole sections out of files at the warning mark into topic files "
        "(a proposal without --apply)",
    )
    cmd.add_argument("--dir", type=Path, default=default_dir, help="one directory per agent")
    cmd.add_argument("--agent", help="only this agent's directory under --dir")
    cmd.add_argument("--file", type=Path, help="only this memory file")
    cmd.add_argument("--apply", action="store_true", help="make the moves and check them")
    cmd.set_defaults(handler=_memory_compact)

    _team_commands(commands)
    return parser


def _team_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    cmd = commands.add_parser(
        "export-case", help="turn a finished ticket into a prospective eval case"
    )
    cmd.add_argument("key", help="ticket key, a directory under $HARNESS_HOME/tickets")
    cmd.add_argument(
        "--source", choices=prospective.ACTUAL_SOURCES, required=True, help="of the actual time"
    )
    cmd.add_argument("--actual-total", help="hours (default: the sum of the parts)")
    cmd.add_argument(
        "--actual-part",
        nargs="+",
        action="extend",
        default=[],
        metavar="KIND=HOURS",
        help=f"actual hours per part kind: {', '.join(prospective.PART_KINDS)}",
    )
    cmd.add_argument(
        "--repo", nargs="+", action="extend", default=[], type=Path, help="read the diff here"
    )
    cmd.add_argument("--grep", help="text the commit messages name (default: the key)")
    cmd.add_argument("--rev", help=REV_HELP)
    cmd.add_argument(
        "--test-pattern",
        action="append",
        help=f"repeatable, replaces the default {' '.join(DEFAULT_TEST_PATTERNS)}",
    )
    cmd.set_defaults(handler=_export_case)

    cmd = commands.add_parser(
        "active-time", help="how long sessions and agents were busy on a ticket, from transcripts"
    )
    cmd.add_argument("key", help="ticket key the work markers name")
    cmd.add_argument(
        "--idle",
        type=float,
        default=activetime.IDLE_SECONDS / 60,
        metavar="MINUTES",
        help="a longer gap between records does not count (default: %(default)g)",
    )
    cmd.add_argument(
        "--away",
        type=float,
        default=activetime.AWAY_SECONDS / 60,
        metavar="MINUTES",
        help="a longer wait for your prompt means you were away (default: %(default)g)",
    )
    cmd.add_argument("--json", action="store_true", help="JSON instead of a line of text")
    cmd.set_defaults(handler=_active_time)

    cmd = commands.add_parser("eval", help="errors of the exported estimates against the actuals")
    cmd.add_argument(
        "directory", type=Path, nargs="?", help="eval cases (default: $HARNESS_HOME/eval-cases)"
    )
    cmd.set_defaults(handler=_eval)

    cmd = commands.add_parser(
        "metrics", help="analyst plan precision and recall, reviewer findings, escaped defects"
    )
    cmd.add_argument(
        "--repo",
        nargs="+",
        action="extend",
        default=[],
        type=Path,
        help="take each ticket's changed files from the commits naming its key here",
    )
    cmd.add_argument("--rev", help=REV_HELP)
    cmd.add_argument("--json", action="store_true", help="JSON instead of Markdown")
    cmd.set_defaults(handler=_metrics)

    cmd = commands.add_parser(
        "review-finding", help="record what the person did with review findings"
    )
    cmd.add_argument("key")
    cmd.add_argument("finding", nargs="+", help="finding ids, e.g. F1")
    cmd.add_argument("--disposition", choices=DISPOSITIONS, required=True)
    cmd.add_argument(
        "--round",
        type=int,
        dest="round_",
        metavar="N",
        help="review round, 1 first (default: the only round with these ids)",
    )
    cmd.add_argument("--note", help="why, in one line")
    cmd.set_defaults(handler=_review_finding)

    cmd = commands.add_parser(
        "escaped", help="link a production defect to the ticket of the commit that caused it"
    )
    cmd.add_argument("--commit", required=True)
    cmd.add_argument("--repo", type=Path, required=True)
    cmd.add_argument(
        "--source", choices=SOURCES, help="default: incident-scout with --case, else manual"
    )
    cmd.add_argument("--ref", help="the Sentry issue id or the incident-scout case id")
    cmd.add_argument("--summary", help="the defect in one line")
    cmd.add_argument("--case", type=Path, help="an incident-scout case file (scout export-case)")
    cmd.add_argument("--ticket", help="the key, when neither the commit nor its merge names one")
    cmd.set_defaults(handler=_escaped)


def _run(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    platform, sources = config.scope(args.platform)
    _refuse_mixed_platforms(config, args.platform, sources)
    store = Store(config.index_path)
    searcher = build_searcher(config, store)
    targets = load_targets(
        store, args.created_from, args.created_to, args.max_hours, sources=sources
    )
    targets = sample(targets, args.sample, args.seed)
    estimators = build_estimators(args, searcher, sources)
    warning = claim_directory(args.out, _manifest(config, platform, sources))
    if warning:
        print(warning, file=sys.stderr)
    print(
        f"platform {platform or ALL_PLATFORMS}: {len(targets)} tickets, "
        f"variants: {[e.name for e in estimators]}",
        file=sys.stderr,
    )
    run(estimators, targets, args.out, progress=_progress)
    print(file=sys.stderr)
    return 0


def _load_config(path: Path | None) -> Config:
    """A named config that is missing must not fall back to every source of the default index."""
    named_by = "--config"
    if path is None and os.environ.get("TICKET_PRECEDENT_CONFIG"):
        path, named_by = default_config_path(), "TICKET_PRECEDENT_CONFIG"
    if path is None or path.exists():
        return load_config(path)
    raise ValueError(f"{named_by} names {path}, which does not exist")


def _manifest(
    config: Config, platform: str | None, sources: frozenset[str] | None
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "platform": platform or ALL_PLATFORMS,
        "sources": None,
        "index_path": str(config.index_path.resolve()),
    }
    if sources is not None:
        manifest["sources"] = sorted(sources)
    return manifest


def _refuse_mixed_platforms(
    config: Config, platform: str | None, sources: frozenset[str] | None
) -> None:
    known = sorted({item.platform for item in config.sources if item.platform})
    if sources is not None or platform == ALL_PLATFORMS or not known:
        return
    raise ValueError(
        f"No platform chosen and the sources have platforms {known}: give --platform NAME, "
        f"--platform {ALL_PLATFORMS} to mix them, or set default_platform in the config"
    )


def build_estimators(
    args: argparse.Namespace, searcher: Searcher, sources: frozenset[str] | None = None
) -> list[Estimator]:
    estimators: list[Estimator] = []
    memory: Memory | None = None
    for variant in (name.strip() for name in args.variants.split(",") if name.strip()):
        with_memory = variant.endswith(WITH_MEMORY)
        context = variant.removesuffix(WITH_MEMORY)
        structured = context.endswith(STRUCTURED)
        context = context.removesuffix(STRUCTURED)
        if context not in LLM_CONTEXTS:
            estimators.append(baseline(variant, args, searcher, sources))
            continue
        if not args.backend:
            raise ValueError(f"{variant} needs at least one --backend")
        if with_memory and args.memory is None:
            raise ValueError(f"{variant} needs --memory DIR")
        if with_memory and memory is None:
            memory = load_memory(args.memory)
        estimators += [
            LLMEstimator(
                backend(spec),
                searcher,
                context=context.removeprefix("llm-").removesuffix("-recent"),
                k=args.k,
                since=args.change if context.endswith("-recent") else None,
                sources=sources,
                process_note=args.process_note,
                structured=structured,
                memory=memory if with_memory else None,
            )
            for spec in args.backend
        ]
    return estimators


def baseline(
    variant: str,
    args: argparse.Namespace,
    searcher: Searcher,
    sources: frozenset[str] | None = None,
) -> Estimator:
    if variant == "neighbour-median":
        return NeighbourMedian(searcher, k=args.k, sources=sources)
    if variant == "recent-median":
        return NeighbourMedian(
            searcher, k=args.k, since=args.change, sources=sources, name="recent-median"
        )
    if variant == "era-scaled-median":
        return EraScaledMedian(searcher, k=args.k, change=args.change, sources=sources)
    if variant == "human":
        return HumanEstimate()
    raise ValueError(f"Unknown variant {variant!r}")


def backend(spec: str) -> Backend:
    kind, _, model = spec.partition(":")
    if kind == "claude":
        return ClaudeCode(model=model or "sonnet")
    if kind == "ollama":
        return Ollama(model=model or "gemma4:12b")
    raise ValueError(f"Unknown backend {spec!r}, expected claude:MODEL or ollama:MODEL")


def _progress(variant: str, done: int, total: int) -> None:
    print(f"\r{variant}: {done}/{total}", end="", file=sys.stderr, flush=True)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _part(text: str) -> tuple[str, float]:
    kind, separator, value = text.partition("=")
    if not separator:
        raise ValueError(f"Expected KIND=HOURS, got {text!r}")
    return kind, prospective.hours(value)


def _export_case(args: argparse.Namespace) -> int:
    directory = ticket_dir(args.key)
    estimate = read_object(directory / "estimate.json")
    if estimate is None:
        raise ValueError(f"No estimate.json in {directory}")
    total = None if args.actual_total is None else prospective.hours(args.actual_total)
    actual = prospective.actual(total, [_part(text) for text in args.actual_part], args.source)
    found_diff = None
    if args.repo:
        term = args.grep or args.key
        found = [change for repo in args.repo for change in changes(repo, term, args.rev)]
        if not found:
            print(f"warning: no commit names {term} in the repositories", file=sys.stderr)
        found_diff = diff(found, args.test_pattern or DEFAULT_TEST_PATTERNS).as_json()
    analysis = read_object(directory / "analysis.json")
    step = estimate_step(os.environ)
    case = prospective.eval_case(args.key, estimate, analysis, actual, found_diff, step)
    active = activetime.measure(args.key)
    if active.total_seconds:
        case["active_time"] = active.as_json() | {
            "total_hours_by_idle": activetime.sensitivity(args.key)
        }
    target = harness_home() / "eval-cases" / f"{args.key}.json"
    write_json(target, case)
    outcome = {"ticket": args.key, "actual": actual, "diff": found_diff, "exported_at": _now()}
    if "active_time" in case:
        outcome["active_time"] = case["active_time"]
    write_json(directory / "outcome.json", outcome)
    estimated = prospective.case_total(case)
    stated = prospective.stated_total(estimate)
    if stated is not None and stated != estimated:
        print(
            f"warning: the estimate states {stated:g} h but computes to {estimated:g} h at the "
            f"export step {step:g} h; the hook may have used another step (set "
            "HARNESS_ESTIMATE_STEP for the export)",
            file=sys.stderr,
        )
    kept = " (stated total, the estimate check failed)" if case.get("check_errors") else ""
    print(
        f"{args.key}: estimate {estimated:g} h{kept}, actual {actual['total']:g} h; wrote {target}"
    )
    return 0


def _eval(args: argparse.Namespace) -> int:
    directory = args.directory or harness_home() / "eval-cases"
    print(prospective.report(prospective.load_cases(directory)), end="")
    return 0


def files_by_key(
    repos: Sequence[Path], keys: Sequence[str], rev: str | None
) -> dict[str, list[str]]:
    """The files the commits naming each key changed, one `git log` per repository."""
    found: dict[str, list[FileChange]] = {key: [] for key in keys}
    for repo in repos:
        for key, repo_changes in changes_by_term(repo, keys, rev).items():
            found[key] += repo_changes
    return {key: list(diff(key_changes).files) for key, key_changes in found.items()}


def _active_time(args: argparse.Namespace) -> int:
    if args.idle <= 0 or args.away <= 0:
        raise ValueError("--idle and --away must be positive numbers of minutes")
    away = args.away * 60
    active = activetime.measure(args.key, idle=args.idle * 60, away=away)
    data = active.as_json() | {"total_hours_by_idle": activetime.sensitivity(args.key, away=away)}
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    if not active.total_seconds:
        print(f"{args.key}: no active time; no work marker or ticket run names it")
        return 0
    by_idle = " / ".join(f"{hours:g}" for hours in data["total_hours_by_idle"].values())
    minutes = "/".join(data["total_hours_by_idle"])
    print(
        f"{args.key}: {data['total_hours']:g} h active (lead {data['lead_hours']:g} h, "
        f"agents {data['agent_hours']:g} h, overlaps counted once) in {active.sessions} "
        f"session(s) and {active.agents} agent run(s), {data['first']} to {data['last']}"
    )
    print(
        f"you: {data['person_hours']:g} h between a reply and your next prompt, "
        f"{data['gate_hours']:g} h of it answering questions and approving plans; "
        f"waits over {args.away:g} min count as away"
    )
    print(f"active with a {minutes} min idle threshold: {by_idle} h")
    if not active.sessions:
        print("no work marker names it, so the lead session's and your time are missing")
    return 0


def _metrics(args: argparse.Namespace) -> int:
    directories = ticket_dirs()
    from_git = None
    if args.repo:
        files = files_by_key(args.repo, [directory.name for directory in directories], args.rev)
        from_git = files.__getitem__
    metrics = team.collect(directories, from_git)
    if args.json:
        print(json.dumps(team.as_json(metrics), indent=2, ensure_ascii=False))
        return 0
    print(team.markdown(metrics), end="")
    return 0


def _review_finding(args: argparse.Namespace) -> int:
    updated = set_disposition(
        ticket_dir(args.key), args.finding, args.disposition, args.note, args.round_
    )
    for finding in updated:
        print(f"{args.key} {finding['id']}: {finding['disposition']}")
    return 0


def _escaped(args: argparse.Namespace) -> int:
    commit = resolve(args.repo, args.commit)
    case = incident_case(args.case) if args.case else None
    key, found_in = args.ticket, "--ticket"
    if not key:
        attribution = ticket_of(args.repo, commit)
        if attribution is None:
            raise ValueError(
                f"Neither {commit[:12]} nor the merge that brought it in names a ticket; "
                "give --ticket KEY"
            )
        key, found_in = pick_ticket(attribution.candidates), attribution.found_in
    directory = ticket_dir(key)
    entry = {
        "commit": commit,
        "ticket": key,
        "source": args.source or ("incident-scout" if case else "manual"),
        "ref": args.ref or (case.ref if case else None),
        "summary": one_line(args.summary or (case.summary if case else "")),
        "recorded_at": _now(),
    }
    if not append_escaped(directory, entry):
        print(f"{key}: {commit[:12]} with this reference is already recorded")
        return 0
    print(f"{key} (from {found_in}): recorded escaped defect {commit[:12]}")
    return 0


def _report(args: argparse.Namespace) -> int:
    runs = load_run(args.directory)
    if not runs:
        raise ValueError(f"No results in {args.directory}")
    calibrated = None
    if args.conformal is not None:
        try:
            window = _window(args.conformal)
        except argparse.ArgumentTypeError as error:
            raise ValueError(f"--conformal: {error}") from None
        calibrated = _calibrated(runs, window, _load_config(args.config).index_path)
    print(report(runs, baseline=args.baseline, calibrated=calibrated), end="")
    return 0


def _window(text: str) -> int | None:
    if text == "all":
        return None
    try:
        window = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a number of tickets or all, got {text!r}"
        ) from None
    if window < 1:
        raise argparse.ArgumentTypeError(f"a window holds at least one ticket, got {window}")
    return window


def _finishes(index: Path, records: Sequence[Record]) -> conformal.Finishes:
    with closing(conformal.open_index(index)) as db:
        return conformal.finish_times(db, [record.id for record in records])


def _calibrated(
    runs: dict[str, list[Record]], window: int | None, index: Path
) -> dict[str, conformal.Evaluation]:
    finishes = _finishes(index, [record for records in runs.values() for record in records])
    calibrated = {}
    for variant, records in runs.items():
        pairs = conformal.prepare(records, finishes).pairs
        if pairs:
            calibrated[variant] = conformal.rolling(pairs, window)
    return calibrated


def _variant_records(path: Path, variant: str) -> tuple[str, list[Record]]:
    if path.is_dir():
        return variant, load_records(run_file(path, variant))
    records = load_records(path)
    return (records[0].variant if records else variant), records


def _calibrate(args: argparse.Namespace) -> int:
    variant, records = _variant_records(args.run, args.variant)
    if not records:
        raise ValueError(f"No {variant} results in {args.run}")
    fit_on = load_records(args.fit_on) if args.fit_on else []
    finishes = _finishes(_load_config(args.config).index_path, [*records, *fit_on])
    prepared = conformal.prepare(records, finishes)
    pool = conformal.prepare(fit_on, finishes).pairs if args.fit_on else None
    windows = sorted({*conformal.WINDOWS, args.window}, key=conformal.size)
    by_window = [
        {
            base: conformal.rolling(
                prepared.pairs, window, args.coverage, p90_base=base, fit_on=pool
            )
            for base in conformal.BASES
        }
        for window in windows
    ]
    ranges = [by_base[conformal.P90_BASE] for by_base in by_window]
    print(_calibrate_intro(variant, prepared, ranges[0], args.coverage))
    print(window_tables(by_window) + "\n")
    chosen = conformal.recommend(ranges)
    print(
        f"Recommended: {window_name(chosen.window)} (inside "
        f"{chosen.shares(after=True).inside:.0%}, closest to {args.coverage:.0%}; ties go to "
        "the narrower range)."
    )
    for each in conformal.BASES:
        smallest = conformal.smallest_stable(by_base[each] for by_base in by_window)
        found = "none" if smallest is None else window_name(smallest.window)
        print(f"P90 from {each.upper()}: smallest window with stable coverage: {found}.")
    window = chosen.window if args.window is None else args.window
    evaluation = ranges[windows.index(window)]
    if evaluation.latest is None:
        print("Too few finished tickets for a current calibration.")
        return 0
    stable = conformal.p90_stable(evaluation)
    latest = evaluation.latest if stable else replace(evaluation.latest, p90_factor=None)
    print(_describe(latest, window))
    if args.write is None:
        return 0
    data = conformal.as_json(latest, window) | {
        "variant": variant,
        "source_run": str(args.run),
        "fitted_on": str(args.fit_on) if args.fit_on else None,
        "p90_refused": None if stable else f"P90 coverage is not stable at {window_name(window)}",
        "rolling": conformal.measured(evaluation),
        "written_at": _now(),
    }
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Wrote {args.write}")
    return 0


def _calibrate_intro(
    variant: str, prepared: conformal.Prepared, first: conformal.Evaluation, coverage: float
) -> str:
    return (
        f"{variant}: {prepared.records} tickets, {len(prepared.pairs)} with a usable range, "
        f"{prepared.without_finish} of them not in the index, {prepared.from_updated} finished "
        "at their last update (no worklog date).\n"
        f"Rolling split-conformal at {coverage:.0%} coverage, calibrated only on tickets "
        f"finished before each ticket was created: {len(first.outcomes)} scored, "
        f"{first.skipped} skipped with fewer than {conformal.MINIMUM} calibration tickets.\n"
    )


def _describe(calibration: conformal.Calibration, window: int | None) -> str:
    p90 = "P90 refused: its coverage is not stable at this window"
    if calibration.p90_factor is not None:
        p90 = f"P90 = {calibration.p90_base.upper()} x{calibration.p90_factor:.2f}"
    return (
        f"Current calibration, {window_name(window)} ({calibration.n} tickets finished "
        f"{calibration.finished_from[:10]} to {calibration.finished_to[:10]}): "
        f"P25 x{calibration.lower_factor:.2f}, P75 x{calibration.upper_factor:.2f}, {p90}."
    )


def _memory_lint(args: argparse.Namespace) -> int:
    root = args.dir.expanduser()
    settings = memory.load_settings()
    results = memory.lint(root, settings, date.today())
    print(memory.render_lint(results, root, settings), end="")
    return 1 if memory.has_findings(results) else 0


def _memory_prune(args: argparse.Namespace) -> int:
    print(memory.prune(args.dir.expanduser(), date.today(), apply=args.apply), end="")
    return 0


def _memory_compact(args: argparse.Namespace) -> int:
    plan = memory.plan_compaction(
        args.dir.expanduser(), memory.load_settings(), agent=args.agent, file=args.file
    )
    print(memory.render_plan(plan), end="")
    if not plan.moves:
        return 0
    if not args.apply:
        print("Proposal only: add --apply to make the moves.")
        return 0
    memory.apply_plan(plan)
    print(f"Moved and checked: {len(plan.moves)} sections, lessons and lines unchanged.")
    return 0

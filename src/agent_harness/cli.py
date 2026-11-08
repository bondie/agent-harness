from __future__ import annotations

import argparse
import functools
import json
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from ticket_precedent.config import build_searcher, load_config
from ticket_precedent.search import Searcher
from ticket_precedent.store import Store

from . import memory, prospective, team
from .artifacts import (
    DISPOSITIONS,
    SOURCES,
    append_escaped,
    harness_home,
    incident_case,
    one_line,
    read_object,
    set_disposition,
    ticket_dir,
    ticket_dirs,
    write_json,
)
from .baselines import EraScaledMedian, Estimator, HumanEstimate, NeighbourMedian
from .dataset import load_targets, sample
from .gitlog import DEFAULT_TEST_PATTERNS, changes, diff, resolve, ticket_of
from .llm import STRUCTURED, WITH_MEMORY, Backend, ClaudeCode, LLMEstimator, Ollama
from .memory import Memory, load_memory
from .report import report
from .runner import load_run, run

BASELINES = ("neighbour-median", "recent-median", "era-scaled-median", "human")
LLM_CONTEXTS = ("llm-none", "llm-keyword", "llm-hybrid", "llm-hybrid-recent")


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
    cmd.add_argument("--memory", type=Path, help="agent memory directory for +memory variants")
    cmd.set_defaults(handler=_run)

    cmd = commands.add_parser("report", help="aggregate Markdown report of a run")
    cmd.add_argument("directory", type=Path)
    cmd.add_argument("--baseline", default="neighbour-median")
    cmd.set_defaults(handler=_report)

    group = commands.add_parser("memory", help="check the lessons in the agents' memory")
    tasks = group.add_subparsers(required=True)
    default_dir = Path.home() / ".claude" / "agent-memory"
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
    cmd.add_argument(
        "--test-pattern",
        action="append",
        help=f"repeatable, replaces the default {' '.join(DEFAULT_TEST_PATTERNS)}",
    )
    cmd.set_defaults(handler=_export_case)

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
    cmd.add_argument("--json", action="store_true", help="JSON instead of Markdown")
    cmd.set_defaults(handler=_metrics)

    cmd = commands.add_parser(
        "review-finding", help="record what the person did with review findings"
    )
    cmd.add_argument("key")
    cmd.add_argument("finding", nargs="+", help="finding ids, e.g. F1")
    cmd.add_argument("--disposition", choices=DISPOSITIONS, required=True)
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
    config = load_config(args.config)
    store = Store(config.index_path)
    searcher = build_searcher(config, store)
    targets = load_targets(store, args.created_from, args.created_to, args.max_hours)
    targets = sample(targets, args.sample, args.seed)
    estimators = build_estimators(args, searcher)
    print(f"{len(targets)} tickets, variants: {[e.name for e in estimators]}", file=sys.stderr)
    run(estimators, targets, args.out, progress=_progress)
    print(file=sys.stderr)
    return 0


def build_estimators(args: argparse.Namespace, searcher: Searcher) -> list[Estimator]:
    estimators: list[Estimator] = []
    memory: Memory | None = None
    for variant in (name.strip() for name in args.variants.split(",") if name.strip()):
        with_memory = variant.endswith(WITH_MEMORY)
        context = variant.removesuffix(WITH_MEMORY)
        structured = context.endswith(STRUCTURED)
        context = context.removesuffix(STRUCTURED)
        if context not in LLM_CONTEXTS:
            estimators.append(baseline(variant, args, searcher))
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
                process_note=args.process_note,
                structured=structured,
                memory=memory if with_memory else None,
            )
            for spec in args.backend
        ]
    return estimators


def baseline(variant: str, args: argparse.Namespace, searcher: Searcher) -> Estimator:
    if variant == "neighbour-median":
        return NeighbourMedian(searcher, k=args.k)
    if variant == "recent-median":
        return NeighbourMedian(searcher, k=args.k, since=args.change, name="recent-median")
    if variant == "era-scaled-median":
        return EraScaledMedian(searcher, k=args.k, change=args.change)
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
        found = [change for repo in args.repo for change in changes(repo, term)]
        if not found:
            print(f"warning: no commit names {term} in the repositories", file=sys.stderr)
        found_diff = diff(found, args.test_pattern or DEFAULT_TEST_PATTERNS).as_json()
    analysis = read_object(directory / "analysis.json")
    case = prospective.eval_case(args.key, estimate, analysis, actual, found_diff)
    target = harness_home() / "eval-cases" / f"{args.key}.json"
    write_json(target, case)
    outcome = {"ticket": args.key, "actual": actual, "diff": found_diff, "exported_at": _now()}
    write_json(directory / "outcome.json", outcome)
    estimated = prospective.estimate_total(estimate)
    print(f"{args.key}: estimate {estimated:g} h, actual {actual['total']:g} h; wrote {target}")
    return 0


def _eval(args: argparse.Namespace) -> int:
    directory = args.directory or harness_home() / "eval-cases"
    print(prospective.report(prospective.load_cases(directory)), end="")
    return 0


def _files_in(repos: Sequence[Path], key: str) -> list[str]:
    return list(diff([change for repo in repos for change in changes(repo, key)]).files)


def _metrics(args: argparse.Namespace) -> int:
    from_git = functools.partial(_files_in, args.repo) if args.repo else None
    metrics = team.collect(ticket_dirs(), from_git)
    if args.json:
        print(json.dumps(team.as_json(metrics), indent=2, ensure_ascii=False))
        return 0
    print(team.markdown(metrics), end="")
    return 0


def _review_finding(args: argparse.Namespace) -> int:
    updated = set_disposition(ticket_dir(args.key), args.finding, args.disposition, args.note)
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
        key, found_in = attribution.ticket, attribution.found_in
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
    print(report(runs, baseline=args.baseline), end="")
    return 0


def _memory_lint(args: argparse.Namespace) -> int:
    root = args.dir.expanduser()
    settings = memory.load_settings()
    results = memory.lint(root, settings, date.today())
    print(memory.render_lint(results, root, settings), end="")
    return 1 if memory.has_findings(results) else 0


def _memory_prune(args: argparse.Namespace) -> int:
    print(memory.prune(args.dir.expanduser(), date.today(), apply=args.apply), end="")
    return 0

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from ticket_precedent.config import build_searcher, load_config
from ticket_precedent.search import Searcher
from ticket_precedent.store import Store

from .baselines import EraScaledMedian, Estimator, HumanEstimate, NeighbourMedian
from .dataset import load_targets, sample
from .llm import Backend, ClaudeCode, LLMEstimator, Ollama
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
        prog="harness-eval", description="Backtest effort estimators on finished tickets."
    )
    commands = parser.add_subparsers(required=True)

    cmd = commands.add_parser("run", help="estimate finished tickets, resuming a previous run")
    cmd.add_argument("--out", type=Path, required=True, help="run directory (kept private)")
    cmd.add_argument("--from", dest="created_from", required=True, help="YYYY-MM-DD")
    cmd.add_argument("--to", dest="created_to", required=True, help="YYYY-MM-DD, exclusive")
    cmd.add_argument(
        "--variants",
        default=",".join(BASELINES),
        help=f"comma-separated: {', '.join(BASELINES + LLM_CONTEXTS)}",
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
    cmd.set_defaults(handler=_run)

    cmd = commands.add_parser("report", help="aggregate Markdown report of a run")
    cmd.add_argument("directory", type=Path)
    cmd.add_argument("--baseline", default="neighbour-median")
    cmd.set_defaults(handler=_report)
    return parser


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
    for variant in (name.strip() for name in args.variants.split(",") if name.strip()):
        if variant in LLM_CONTEXTS:
            if not args.backend:
                raise ValueError(f"{variant} needs at least one --backend")
            estimators += [
                LLMEstimator(
                    backend(spec),
                    searcher,
                    context=variant.removeprefix("llm-").removesuffix("-recent"),
                    k=args.k,
                    since=args.change if variant.endswith("-recent") else None,
                    process_note=args.process_note,
                )
                for spec in args.backend
            ]
            continue
        estimators.append(baseline(variant, args, searcher))
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


def _report(args: argparse.Namespace) -> int:
    runs = load_run(args.directory)
    if not runs:
        raise ValueError(f"No results in {args.directory}")
    print(report(runs, baseline=args.baseline), end="")
    return 0

"""Run estimators over backtest targets, one JSON line per prediction, resumable."""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from .baselines import Estimator
from .dataset import Target
from .llm import LLMError

RECOVERABLE = (LLMError, httpx.HTTPError, subprocess.TimeoutExpired)
MANIFEST = "manifest.json"


@dataclass(frozen=True)
class Record:
    variant: str
    id: str
    project: str
    issue_type: str
    created: str
    actual: float
    human: float | None
    median: float | None = None
    p25: float | None = None
    p75: float | None = None
    cited: tuple[str, ...] = ()
    cost_usd: float = 0.0
    seconds: float = 0.0
    note: str = ""
    error: str = ""
    reference_median: float | None = None
    lessons: int | None = None
    lessons_dated: int | None = None
    lessons_undated: int | None = None
    block_mode: str | None = None
    block_reference_median: float | None = None
    retried: bool = False
    check_errors: tuple[str, ...] = ()

    @classmethod
    def load(cls, line: str) -> Record:
        data: dict[str, Any] = json.loads(line)
        data["cited"] = tuple(data.get("cited") or ())
        data["check_errors"] = tuple(data.get("check_errors") or ())
        return cls(**data)


def target_id(target: Target) -> str:
    return f"{target.source}/{target.key}"


def run_file(directory: Path, variant: str) -> Path:
    return directory / f"{re.sub(r'[^A-Za-z0-9.@-]+', '_', variant)}.jsonl"


def claim_directory(directory: Path, manifest: dict[str, Any]) -> str | None:
    """Keep a resume from mixing platforms or indexes; directories from before manifests warn."""
    path = directory / MANIFEST
    if path.exists():
        _check_manifest(directory, json.loads(path.read_text()), manifest)
        return None
    if any(directory.glob("*.jsonl")):
        return f"warning: {directory} has no {MANIFEST}, so its platform and index are not checked"
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return None


def _check_manifest(directory: Path, recorded: dict[str, Any], manifest: dict[str, Any]) -> None:
    changed = [key for key, value in manifest.items() if recorded.get(key) != value]
    if not changed:
        return
    then = ", ".join(f"{key} {recorded.get(key)!r}" for key in changed)
    now = ", ".join(f"{key} {manifest[key]!r}" for key in changed)
    raise ValueError(f"{directory} was run with {then}; this run has {now}: use another --out")


def run(
    estimators: Sequence[Estimator],
    targets: Sequence[Target],
    directory: Path,
    progress: Callable[[str, int, int], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for estimator in estimators:
        path = run_file(directory, estimator.name)
        done = {record.id for record in load_records(path) if not record.error}
        todo = [target for target in targets if target_id(target) not in done]
        with path.open("a") as out:
            for position, target in enumerate(todo, start=1):
                out.write(json.dumps(asdict(predict(estimator, target, clock))) + "\n")
                out.flush()
                if progress:
                    progress(estimator.name, position, len(todo))


def predict(estimator: Estimator, target: Target, clock: Callable[[], float]) -> Record:
    base = Record(
        variant=estimator.name,
        id=target_id(target),
        project=target.project,
        issue_type=target.issue_type,
        created=target.created,
        actual=target.actual_hours,
        human=target.human_hours,
    )
    started = clock()
    try:
        estimate = estimator.estimate(target)
    except RECOVERABLE as error:
        cost = error.cost_usd if isinstance(error, LLMError) else 0.0
        return _replace(base, seconds=clock() - started, error=str(error)[:500], cost_usd=cost)
    seconds = clock() - started
    if estimate is None:
        return _replace(base, seconds=seconds, error="no estimate")
    return _replace(
        base,
        median=estimate.median,
        p25=estimate.p25,
        p75=estimate.p75,
        cited=estimate.cited,
        cost_usd=estimate.cost_usd,
        seconds=seconds,
        note=estimate.note,
        reference_median=estimate.reference_median,
        lessons=estimate.lessons,
        lessons_dated=estimate.lessons_dated,
        lessons_undated=estimate.lessons_undated,
        block_mode=estimate.block_mode,
        block_reference_median=estimate.block_reference_median,
        retried=estimate.retried,
        check_errors=estimate.check_errors,
    )


def _replace(record: Record, **changes: Any) -> Record:
    return Record(**{**asdict(record), **changes})


def load_records(path: Path) -> list[Record]:
    """The latest record per ticket: a retried error is superseded by its success."""
    if not path.exists():
        return []
    latest: dict[str, Record] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            record = Record.load(line)
            latest[record.id] = record
    return list(latest.values())


def load_run(directory: Path) -> dict[str, list[Record]]:
    runs: dict[str, list[Record]] = {}
    for path in sorted(directory.glob("*.jsonl")):
        records = load_records(path)
        if records:
            runs[records[0].variant] = records
    return runs

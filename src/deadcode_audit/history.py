"""Score history + ``trend`` (aislop's ``history.jsonl`` + ASCII sparkline).

Only terminal-mode scans append one compact record per run to ``evals/deadcode/score-history.jsonl``;
machine outputs (``--json``/``--sarif``/``--prompt``) and the ``ci`` gate never write history, so
automated runs keep the human trend clean. ``trend`` reads the file and prints a table plus a
sparkline of recent scores; a corrupt line is skipped with a warning rather than failing the read.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from deadcode_audit import diffscope

HISTORY_PATH = Path("evals/deadcode/score-history.jsonl")
_SPARK_TICKS = "▁▂▃▄▅▆▇█"


@dataclass(frozen=True)
class HistoryRecord:
    """One scan's compact, machine-readable summary."""

    timestamp: str
    score: int
    label: str
    errors: int
    warnings: int
    info: int
    files: int


def record_to_dict(record: HistoryRecord) -> dict[str, object]:
    return {
        "timestamp": record.timestamp,
        "score": record.score,
        "label": record.label,
        "errors": record.errors,
        "warnings": record.warnings,
        "info": record.info,
        "files": record.files,
    }


def append_record(record: HistoryRecord, path: Path | None = None) -> None:
    """Append one record as a JSON line, creating the history file/dir if needed.

    The trend file is a local single-writer artifact: each record is one small append and there
    is no cross-process lock, so two truly simultaneous scans could interleave. The reader
    tolerates a torn line (:func:`read_records`), which bounds the damage to one record.
    """
    target = path or (diffscope.REPO_ROOT / HISTORY_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record_to_dict(record), sort_keys=True) + "\n")


def _parse_record(line: str) -> HistoryRecord | None:
    """Parse one JSONL line into a record, or return ``None`` when the line is unusable.

    A fallback-value return keeps the failure decision at the call site: :func:`read_records`
    owns the recovery policy (warn with line context and skip) instead of the handler silently
    continuing past the error.
    """
    try:
        data = json.loads(line)
        return HistoryRecord(
            timestamp=str(data["timestamp"]),
            score=int(data["score"]),
            label=str(data["label"]),
            errors=int(data.get("errors", 0)),
            warnings=int(data.get("warnings", 0)),
            info=int(data.get("info", 0)),
            files=int(data.get("files", 0)),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def read_records(path: Path | None = None) -> list[HistoryRecord]:
    """Read all history records (empty list if the file does not exist).

    A corrupt line (torn append, manual edit, schema drift) is skipped with a stderr warning
    instead of failing the whole trend read — one bad line must not erase the history view.
    """
    target = path or (diffscope.REPO_ROOT / HISTORY_PATH)
    if not target.exists():
        return []
    records: list[HistoryRecord] = []
    for lineno, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record = _parse_record(line)
        if record is None:
            print(f"warning: skipping corrupt history line {lineno}", file=sys.stderr)
            continue
        records.append(record)
    return records


def sparkline(scores: list[int]) -> str:
    """Render scores (0–100) as a fixed-scale ASCII sparkline (0=▁ … 100=█)."""
    if not scores:
        return ""
    return "".join(_SPARK_TICKS[min(len(_SPARK_TICKS) - 1, max(0, s) * (len(_SPARK_TICKS) - 1) // 100)] for s in scores)


def render_trend(records: list[HistoryRecord], limit: int = 20) -> str:
    """Render a recent-history table + sparkline of the last ``limit`` scores (``limit <= 0`` shows all)."""
    if not records:
        return "no score history yet (run `python -m deadcode_audit scan` to record one)"
    # ``records[-0:]`` would silently be the whole list, so make the limit<=0 = "all" semantics
    # explicit (matching the other ``--top/--limit 0 = all`` subcommands).
    recent = records[-limit:] if limit > 0 else records
    lines = [f"score trend (last {len(recent)} of {len(records)}):  {sparkline([r.score for r in recent])}", ""]
    for record in recent:
        lines.append(
            f"  {record.timestamp}  {record.score:>3}/100  {record.label:<11} "
            f"(E{record.errors} W{record.warnings} I{record.info}, {record.files} files)"
        )
    return "\n".join(lines)

"""Score history + ``trend`` (aislop's ``history.jsonl`` + ASCII sparkline).

A normal terminal scan appends one compact record per run to ``evals/deadcode/score-history.jsonl``
(never for ``--json``/``--sarif`` output, so machine output stays clean). ``trend`` reads it and
prints a table plus a sparkline of recent scores.
"""

from __future__ import annotations

import json
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
    """Append one record as a JSON line, creating the history file/dir if needed."""
    target = path or (diffscope.REPO_ROOT / HISTORY_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record_to_dict(record), sort_keys=True) + "\n")


def read_records(path: Path | None = None) -> list[HistoryRecord]:
    """Read all history records (empty list if the file does not exist)."""
    target = path or (diffscope.REPO_ROOT / HISTORY_PATH)
    if not target.exists():
        return []
    records: list[HistoryRecord] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        records.append(
            HistoryRecord(
                timestamp=str(data["timestamp"]),
                score=int(data["score"]),
                label=str(data["label"]),
                errors=int(data.get("errors", 0)),
                warnings=int(data.get("warnings", 0)),
                info=int(data.get("info", 0)),
                files=int(data.get("files", 0)),
            )
        )
    return records


def sparkline(scores: list[int]) -> str:
    """Render scores (0–100) as a fixed-scale ASCII sparkline (0=▁ … 100=█)."""
    if not scores:
        return ""
    return "".join(_SPARK_TICKS[min(len(_SPARK_TICKS) - 1, max(0, s) * (len(_SPARK_TICKS) - 1) // 100)] for s in scores)


def render_trend(records: list[HistoryRecord], limit: int = 20) -> str:
    """Render a recent-history table + sparkline of the last ``limit`` scores."""
    if not records:
        return "no score history yet (run `python -m deadcode_audit scan` to record one)"
    recent = records[-limit:]
    lines = [f"score trend (last {len(recent)} of {len(records)}):  {sparkline([r.score for r in recent])}", ""]
    for record in recent:
        lines.append(
            f"  {record.timestamp}  {record.score:>3}/100  {record.label:<11} "
            f"(E{record.errors} W{record.warnings} I{record.info}, {record.files} files)"
        )
    return "\n".join(lines)

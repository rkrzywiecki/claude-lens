"""Aggregate request records into a cost and cache report."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from claude_lens.models import GroupBy, Report, ReportRow, RequestRecord
from claude_lens.pricing import Pricing, normalize_model


def build_report(
    records: Iterable[RequestRecord],
    pricing: Pricing,
    group_by: GroupBy,
    top: int | None = None,
) -> Report:
    """Group records; days are listed in time order, other groups by cost (highest first)."""
    rows: dict[str, ReportRow] = {}
    total = ReportRow(key="total")
    unpriced: set[str] = set()
    for record in records:
        key = group_key(record, group_by)
        row = rows.get(key)
        if row is None:
            row = rows[key] = ReportRow(key=key, label=_label(record, group_by))
        cost = pricing.cost(record.model, record.usage, record.speed)
        for target in (row, total):
            target.requests += 1
            target.usage = target.usage + record.usage
            if cost is None:
                target.unpriced_requests += 1
            else:
                target.cost = target.cost + cost
        if cost is None:
            unpriced.add(record.model)

    ordered = list(rows.values())
    if group_by == "day":
        ordered.sort(key=lambda r: r.key)
    else:
        ordered.sort(key=lambda r: (-r.cost.total, r.key))
    if top is not None:
        ordered = ordered[-top:] if group_by == "day" else ordered[:top]
    return Report(group_by=group_by, rows=ordered, total=total, unpriced_models=sorted(unpriced))


def group_key(record: RequestRecord, group_by: GroupBy) -> str:
    match group_by:
        case "day":
            return record.timestamp.astimezone().date().isoformat()
        case "project":
            return shorten_home(record.project)
        case "model":
            return normalize_model(record.model)
        case "session":
            return record.session_id


def shorten_home(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path


def _label(record: RequestRecord, group_by: GroupBy) -> str | None:
    if group_by != "session":
        return None
    started = record.timestamp.astimezone().strftime("%Y-%m-%d %H:%M")
    return f"{started}  {shorten_home(record.project)}"

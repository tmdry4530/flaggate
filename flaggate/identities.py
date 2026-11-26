"""Identities (identifier + traits) to replay a plan against.

Sources: python records, JSON file, CSV file (``identifier`` column + one column per trait), or the
Flagsmith Admin API (``fetch_identities``, see flagsmith_io.py).
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class Identity:
    identifier: str
    traits: Mapping[str, Any] = field(default_factory=dict)


def coerce_trait(value: Any) -> Any:
    """CSV cells are strings; Flagsmith traits are typed. 'true'/'false' -> bool, '12' -> int, '1.5' -> float."""
    if not isinstance(value, str):
        return value
    v = value.strip()
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return value


def identities_from_records(records: Iterable[Any]) -> list[Identity]:
    """Accepts Identity, {"identifier":..., "traits": {...}}, or flat {"identifier":..., "plan": "pro"}."""
    out: list[Identity] = []
    seen: set[str] = set()
    for r in records:
        if isinstance(r, Identity):
            ident = r
        elif isinstance(r, str):
            ident = Identity(r, {})
        else:
            rec = dict(r)
            identifier = str(rec.pop("identifier"))
            traits = rec.pop("traits", None)
            if traits is None:
                traits = rec
            ident = Identity(identifier, dict(traits))
        if ident.identifier in seen:
            raise ValueError(f"duplicate identity {ident.identifier!r}")
        seen.add(ident.identifier)
        out.append(ident)
    return out


def identities_from_json(path: str | Path) -> list[Identity]:
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        data = data.get("identities", data.get("results"))
    return identities_from_records(data)


def identities_from_csv(path: str | Path) -> list[Identity]:
    with Path(path).open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    recs = []
    for row in rows:
        row = dict(row)
        identifier = row.pop("identifier")
        recs.append({"identifier": identifier,
                     "traits": {k: coerce_trait(v) for k, v in row.items() if k and v not in (None, "")}})
    return identities_from_records(recs)


def load_identities(path: str | Path) -> list[Identity]:
    p = Path(path)
    return identities_from_csv(p) if p.suffix.lower() == ".csv" else identities_from_json(p)

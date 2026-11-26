"""Append-only, hash-chained audit log (JSONL).

record_hash = sha256(prev_hash + canonical(record without record_hash))
The first record chains from GENESIS ("0" * 64). ``verify`` detects edited,
reordered, inserted or deleted records (and a truncated tail if the caller
compares against a known head).
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flaggate.state import sha256_hex

GENESIS = "0" * 64


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record_hash(prev_hash: str, record: dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "record_hash"}
    return sha256_hex(prev_hash + _canon(body))


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _tail(self, f: Any) -> tuple[int, str]:
        f.seek(0)
        seq, last = 0, GENESIS
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                seq, last = r["seq"], r["record_hash"]
        return seq, last

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                seq, prev = self._tail(f)
                rec: dict[str, Any] = {
                    "seq": seq + 1,
                    "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    "event": event,
                    **fields,
                    "prev_hash": prev,
                }
                rec["record_hash"] = record_hash(prev, rec)
                f.write(_canon(rec) + "\n")
                f.flush()
                os.fsync(f.fileno())
                return rec
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def verify(self) -> tuple[bool, int, str]:
        """(ok, number of records, message)."""
        prev, n = GENESIS, 0
        if not self.path.exists():
            return True, 0, "empty log"
        for lineno, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                return False, n, f"line {lineno}: not JSON"
            n += 1
            if r.get("seq") != n:
                return False, n, f"line {lineno}: seq {r.get('seq')} != expected {n} (record removed or reordered)"
            if r.get("prev_hash") != prev:
                return False, n, f"line {lineno}: prev_hash does not match previous record"
            if record_hash(prev, r) != r.get("record_hash"):
                return False, n, f"line {lineno}: record_hash mismatch (record edited)"
            prev = r["record_hash"]
        return True, n, f"chain OK, {n} records, head {prev[:12]}"

    def head(self) -> str:
        recs = self.records()
        return recs[-1]["record_hash"] if recs else GENESIS

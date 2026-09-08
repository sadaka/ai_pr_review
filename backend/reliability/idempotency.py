"""Idempotency helpers for retry-safe side effects.

Two patterns are used in this codebase:
  - **DB-enforced** — a unique index plus `ON CONFLICT DO NOTHING`, so a
    retried write is a no-op (see `finding_records_dedup_idx` and
    `TruthStore.insert_findings`). `fingerprint()` gives a stable content key
    when the natural columns aren't enough.
  - **In-process** — `OnceGuard` remembers which keys a side effect has
    already been applied for within one process, for the window between a
    partial failure and its retry where no DB constraint covers the action.
"""
from __future__ import annotations

import hashlib

_SEP = "\x1f"


def fingerprint(*parts: object) -> str:
    """Stable hex digest of `parts` — order-sensitive, type-normalised via repr."""
    joined = _SEP.join(repr(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


class OnceGuard:
    """`if guard.first_time(key): do_side_effect()` — True once per key, per process."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def first_time(self, key: str) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    def reset(self) -> None:
        self._seen.clear()

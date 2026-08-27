"""Identifiers, clocks and the sort keys that order siblings.

Kept free of database imports so the migration importer and the tests can use
the same generators the services use.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

# Sort keys are compared by SQLite's BINARY collation, i.e. byte order. This
# alphabet is in ascending byte order, so plain string comparison in Python and
# `ORDER BY sort_key` in SQL agree.
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_INDEX = {char: i for i, char in enumerate(ALPHABET)}
BASE = len(ALPHABET)
_MID = BASE // 2

# No key may end in the lowest character. Otherwise one key can be an exact
# prefix of its neighbour ("V002" and "V0020"), and nothing at all sorts between
# those two — the level would need a resequence after a single insert.
_LOWEST = ALPHABET[0]
_FILL = ALPHABET[1]

# A key this long has absorbed too many insert-in-the-middle operations; the
# service resequences that one level instead of letting keys grow without bound.
MAX_RANK_LENGTH = 48

# Sort keys are fixed-width base-62 counters: appending or prepending steps the
# counter and keeps the width, so only a genuine mid-list insert makes a key
# longer. The first key sits a third of the way in, leaving millions of slots on
# either side of it.
RANK_WIDTH = 4
FIRST_RANK = "V" + _LOWEST * (RANK_WIDTH - 2) + _FILL


class RankExhausted(ValueError):
    """No key fits in the requested gap; the caller must resequence the level."""


def new_id() -> str:
    """A UUIDv4 string. Never reused, including for purged rows."""
    return str(uuid.uuid4())


def now_ms() -> int:
    """UTC epoch milliseconds — the only time representation stored."""
    return int(time.time() * 1000)


def to_iso(ms: int | None) -> str | None:
    """Epoch milliseconds as an ISO-8601 UTC string, for API responses."""
    if ms is None:
        return None
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _digits(key: str) -> list[int]:
    try:
        return [_INDEX[char] for char in key]
    except KeyError as exc:
        raise RankExhausted(f"{key!r} is not a sort key") from exc


def rank_after(last: str | None) -> str:
    """The key for a new last sibling: `last` incremented as a base-62 counter.

    Incrementing keeps appends flat in length. Bisecting towards the top of the
    alphabet instead would halve the remaining space every time, growing the key
    by a character every few rows.
    """
    if not last:
        return FIRST_RANK
    digits = _digits(last)
    for i in range(len(digits) - 1, -1, -1):
        if digits[i] + 1 < BASE:
            digits[i] += 1
            for j in range(i + 1, len(digits)):
                digits[j] = 0
            if digits[-1] == 0:
                digits[-1] = 1
            return "".join(ALPHABET[d] for d in digits)
    if len(last) >= MAX_RANK_LENGTH:
        raise RankExhausted(f"key would exceed {MAX_RANK_LENGTH} characters")
    return last + _FILL  # every digit is already at the top; widen instead


def rank_before(first: str | None) -> str:
    """The key for a new first sibling: `first` decremented as a base-62 counter."""
    if not first:
        return FIRST_RANK
    digits = _digits(first)
    last_index = len(digits) - 1
    for i in range(last_index, -1, -1):
        floor = 1 if i == last_index else 0
        if digits[i] > floor:
            digits[i] -= 1
            for j in range(i + 1, len(digits)):
                digits[j] = BASE - 1
            return "".join(ALPHABET[d] for d in digits)
    raise RankExhausted(f"nothing sorts before {first!r}")


def rank_between(before: str | None = None, after: str | None = None) -> str:
    """A sort key strictly between `before` and `after`.

    Either bound may be None for "nothing on that side". Raises `RankExhausted`
    when the gap cannot be filled within `MAX_RANK_LENGTH` — the caller then
    resequences that level inside the same transaction.
    """
    if not before or not after:
        # An open end is a counter step, not a bisection.
        return rank_before(after) if not before else rank_after(before)
    if before >= after:
        raise RankExhausted(f"sort keys out of order: {before!r} >= {after!r}")

    out: list[str] = []
    position = 0
    while len(out) < MAX_RANK_LENGTH:
        # -1 stands for "before the first character": a key that has run out is
        # smaller than any key that continues past it.
        low = _INDEX[before[position]] if position < len(before) else -1
        high = _INDEX[after[position]] if position < len(after) else BASE
        if low + 1 < high:
            out.append(ALPHABET[(low + 1 + high) // 2])
            if out[-1] == _LOWEST:
                # Already strictly below `after` at this position, so any suffix
                # keeps it there — and this one keeps the trailing-zero rule.
                out.append(ALPHABET[_MID])
            key = "".join(out)
            # Belt and braces: a malformed neighbouring key must fail loudly
            # rather than sort wrongly once it is in the table.
            if key <= before or key >= after:
                raise RankExhausted(f"no key fits between {before!r} and {after!r}")
            return key
        # No room at this position; keep the shared prefix and look one deeper.
        out.append(ALPHABET[low] if low >= 0 else _LOWEST)
        position += 1
    raise RankExhausted(f"key would exceed {MAX_RANK_LENGTH} characters")


def resequence(count: int) -> list[str]:
    """`count` evenly spread keys, for rebuilding one level from scratch."""
    if count <= 0:
        return []
    width = RANK_WIDTH
    while BASE ** width < (count + 1) * 4:
        width += 1
    step = (BASE ** width) // (count + 1)
    keys = []
    for i in range(1, count + 1):
        value = i * step
        digits = []
        for _ in range(width):
            digits.append(value % BASE)
            value //= BASE
        digits.reverse()
        if digits[-1] == 0:
            digits[-1] = 1
        keys.append("".join(ALPHABET[d] for d in digits))
    return keys

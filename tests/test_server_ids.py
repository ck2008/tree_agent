"""Sort keys: the ordering the whole tree depends on.

Sibling order is a string, compared byte for byte by SQLite. If two keys can
collide, sort out of order, or leave no room between them, the tree silently
scrambles — so this suite is a stress test rather than a handful of examples.
"""

from __future__ import annotations

import random

from server_support import banner

from tree_agent.server import ids

LOWEST = ids.ALPHABET[0]


def check(keys: list[str], label: str) -> None:
    assert keys == sorted(keys), f"{label}: out of order"
    assert len(set(keys)) == len(keys), f"{label}: duplicate keys"
    # A key ending in the lowest character can be an exact prefix of its
    # neighbour, and nothing at all sorts between those two.
    assert not any(key.endswith(LOWEST) for key in keys), f"{label}: trailing zero"


banner("appending and prepending stay flat in length")
for label in ("append", "prepend"):
    keys: list[str] = []
    for _ in range(20000):
        if label == "append":
            keys.append(ids.rank_between(keys[-1] if keys else None, None))
        else:
            keys.insert(0, ids.rank_between(None, keys[0] if keys else None))
    check(keys, label)
    print(f"{label}: 20000 keys, longest {max(map(len, keys))} chars ({keys[0]} … {keys[-1]})")

banner("random inserts anywhere keep the order")
for seed in range(3):
    random.seed(seed)
    keys = [ids.rank_after(None)]
    for _ in range(10000):
        position = random.randrange(len(keys) + 1)
        keys.insert(
            position,
            ids.rank_between(
                keys[position - 1] if position else None,
                keys[position] if position < len(keys) else None,
            ),
        )
    check(keys, f"random{seed}")
print("30000 random inserts, longest key", max(map(len, keys)), "chars")

banner("splitting one gap over and over asks for a resequence rather than growing forever")
low, high, splits = ids.FIRST_RANK, ids.rank_after(ids.FIRST_RANK), 0
try:
    while True:
        high = ids.rank_between(low, high)
        splits += 1
except ids.RankExhausted as exc:
    print(f"one gap survived {splits} splits, then:", exc)
assert splits > 100, splits

banner("resequencing a level leaves room on both sides of every slot")
for count in (1, 2, 61, 62, 500, 4000):
    keys = ids.resequence(count)
    assert len(keys) == count
    check(keys, f"resequence({count})")
    ids.rank_between(None, keys[0])
    ids.rank_between(keys[-1], None)
    for left, right in zip(keys, keys[1:]):
        ids.rank_between(left, right)
print("resequence(3) =", ids.resequence(3))

banner("bad input fails loudly instead of producing a key that sorts wrongly")
for before, after in ((ids.rank_after(None), ids.FIRST_RANK), ("not a key", "V001")):
    try:
        ids.rank_between(before, after)
    except ids.RankExhausted as exc:
        print("refused:", str(exc)[:60])
    else:
        raise AssertionError(f"expected RankExhausted for {before!r}/{after!r}")

banner("ids and timestamps")
generated = {ids.new_id() for _ in range(10000)}
assert len(generated) == 10000
sample = next(iter(generated))
assert len(sample) == 36 and sample.count("-") == 4, sample
assert ids.to_iso(0) == "1970-01-01T00:00:00.000Z"
assert ids.to_iso(None) is None
assert ids.to_iso(ids.now_ms()).endswith("Z")
print("uuid:", sample, "| now:", ids.to_iso(ids.now_ms()))

print("\ntest_server_ids OK")

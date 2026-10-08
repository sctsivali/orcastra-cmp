"""A Redis stand-in covering exactly what the monitoring history store uses: sorted sets, plain
strings with NX locks, expiry, pipelines and the store's one write script, whose rule is
reimplemented in Python (add a member only when its score is absent, trim by score and by rank,
refresh the expiry)."""

from typing import Any, Dict, List, Optional, Tuple


def _bound(value: Any) -> float:
    if value in ("-inf", "+inf"):
        return float(value)
    text = str(value)
    if text.startswith("("):
        return float(text[1:])
    return float(text)


class FakeRedis:
    def __init__(self) -> None:
        self.zsets: Dict[str, Dict[str, float]] = {}
        self.strings: Dict[str, str] = {}
        self.ttl: Dict[str, int] = {}
        self.calls: List[str] = []
        self.fail_with: Optional[Exception] = None

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_with is not None:
            raise self.fail_with

    def _ordered(self, key: str) -> List[Tuple[str, float]]:
        return sorted(self.zsets.get(key, {}).items(), key=lambda item: (item[1], item[0]))

    # strings
    def get(self, key: str) -> Optional[str]:
        self._check("get")
        return self.strings.get(key)

    def set(self, key: str, value: str, nx: bool = False, ex: Optional[int] = None) -> Optional[bool]:
        self._check("set")
        if nx and (key in self.strings or key in self.zsets):
            return None
        self.strings[key] = value
        if ex is not None:
            self.ttl[key] = int(ex)
        return True

    def exists(self, key: str) -> int:
        self._check("exists")
        return int(key in self.strings or key in self.zsets)

    def delete(self, *keys: str) -> int:
        self._check("delete")
        removed = 0
        for key in keys:
            removed += int(self.strings.pop(key, None) is not None or self.zsets.pop(key, None) is not None)
            self.ttl.pop(key, None)
        return removed

    # sorted sets
    def zcard(self, key: str) -> int:
        self._check("zcard")
        return len(self.zsets.get(key, {}))

    def zcount(self, key: str, low: Any, high: Any) -> int:
        self._check("zcount")
        lo, hi = _bound(low), _bound(high)
        exclusive = str(low).startswith("(")
        return sum(
            1 for _, score in self._ordered(key) if (score > lo if exclusive else score >= lo) and score <= hi
        )

    def zrange(self, key: str, start: int, end: int, withscores: bool = False) -> List[Any]:
        self._check("zrange")
        rows = self._ordered(key)
        stop = len(rows) if end == -1 else (end + 1 if end >= 0 else len(rows) + end + 1)
        begin = start if start >= 0 else max(len(rows) + start, 0)
        picked = rows[begin:stop]
        return [(member, score) for member, score in picked] if withscores else [m for m, _ in picked]

    def zrangebyscore(self, key: str, low: Any, high: Any) -> List[str]:
        self._check("zrangebyscore")
        lo, hi = _bound(low), _bound(high)
        return [member for member, score in self._ordered(key) if lo <= score <= hi]

    def write_points(self, key: str, args: List[Any]) -> int:
        """The write script's rule, in the order the Lua runs it."""
        oldest, most, ttl = float(args[0]), int(args[1]), int(args[2])
        members = self.zsets.setdefault(key, {})
        scores = set(members.values())
        added = 0
        for index in range(3, len(args), 2):
            score, member = float(args[index]), str(args[index + 1])
            if score not in scores:
                members[member] = score
                scores.add(score)
                added += 1
        for member, score in list(members.items()):
            if score < oldest:
                del members[member]
        ordered = self._ordered(key)
        for member, _ in ordered[: max(len(ordered) - most, 0)]:
            del members[member]
        if members:
            self.ttl[key] = ttl
        else:
            self.zsets.pop(key, None)
        return added

    def register_script(self, _source: str) -> "_Script":
        return _Script(self)

    def pipeline(self, transaction: bool = True) -> "_Pipeline":
        return _Pipeline(self)


class _Script:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis

    def __call__(self, keys: List[str], args: List[Any], client: Any = None) -> int:
        self._redis._check("script")
        return self._redis.write_points(keys[0], list(args))


class _Pipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._ops: List[Any] = []

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._redis, name)

        def queue(*args: Any, **kwargs: Any) -> "_Pipeline":
            self._ops.append(lambda: method(*args, **kwargs))
            return self

        return queue

    def execute(self) -> List[Any]:
        self._redis._check("execute")
        results = [op() for op in self._ops]
        self._ops = []
        return results

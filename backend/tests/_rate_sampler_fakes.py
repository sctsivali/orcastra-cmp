"""A Redis stand-in covering exactly what the rate sampler uses: hashes, expiry, pipelines and
one registered compare-and-set script, with the script's rule reimplemented in Python."""

from typing import Any, Dict, List, Optional


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.ttl: Dict[str, int] = {}
        self.script_calls = 0
        self.fail_with: Optional[Exception] = None

    def _check(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def hget(self, key: str, field: str) -> Optional[str]:
        self._check()
        return self.hashes.get(key, {}).get(field)

    def cas(self, key: str, taken_at: str, value: str, ttl: int) -> int:
        self._check()
        self.script_calls += 1
        current = self.hashes.get(key, {}).get("t")
        if current is not None and float(current) >= float(taken_at):
            return 0
        self.hashes[key] = {"t": taken_at, "v": value}
        self.ttl[key] = int(ttl)
        return 1

    def register_script(self, _source: str) -> "_Script":
        return _Script(self)

    def pipeline(self, transaction: bool = True) -> "_Pipeline":
        return _Pipeline(self)


class _Script:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis

    def __call__(self, keys: List[str], args: List[Any], client: Any = None) -> Any:
        if isinstance(client, _Pipeline):
            client.queue(lambda: self._redis.cas(keys[0], str(args[0]), str(args[1]), int(args[2])))
            return client
        return self._redis.cas(keys[0], str(args[0]), str(args[1]), int(args[2]))


class _Pipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._ops: List[Any] = []

    def queue(self, op: Any) -> None:
        self._ops.append(op)

    def hget(self, key: str, field: str) -> "_Pipeline":
        self._ops.append(lambda: self._redis.hget(key, field))
        return self

    def execute(self) -> List[Any]:
        self._redis._check()
        results = [op() for op in self._ops]
        self._ops = []
        return results

"""Process-local cache of publication-bound reads (``api/read_cache.py``, OPS-01).

Keys are chosen by ``forecast_db`` (publication + parameters); a new publication is a new
key. Here: expiry after the TTL, no caching without a key, one computation for
concurrent requests, a failed computation is not stored, the entry bound.
The PostgreSQL path (new generation, decision and check result visible at once) is
``test_read_cache_db.py``.
"""

import threading
import time

import pytest

from infra_pulse_backend.api.read_cache import ReadCache


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_value_is_reused_until_ttl_and_a_new_key_misses():
    clock = Clock()
    cache = ReadCache(ttl_seconds=30, clock=clock)
    calls: list[str] = []

    def compute(value: str):
        def run() -> str:
            calls.append(value)
            return value

        return run

    assert cache.get_or_compute(("list", "generation-1"), compute("a")) == "a"
    clock.now += 29.9
    assert cache.get_or_compute(("list", "generation-1"), compute("b")) == "a"
    # Another publication is another key: read at once, whatever the TTL.
    assert cache.get_or_compute(("list", "generation-2"), compute("c")) == "c"
    clock.now += 0.2
    assert cache.get_or_compute(("list", "generation-1"), compute("d")) == "d"
    assert calls == ["a", "c", "d"]


def test_no_key_is_never_cached():
    cache = ReadCache()
    values = iter(range(3))
    assert [cache.get_or_compute(None, lambda: next(values)) for _ in range(3)] == [0, 1, 2]
    assert len(cache) == 0


def test_cached_none_is_a_hit():
    cache = ReadCache()
    calls = []
    for _ in range(2):
        assert cache.get_or_compute("unknown-object", lambda: calls.append(1)) is None
    assert calls == [1]


def test_concurrent_requests_share_one_computation():
    cache = ReadCache()
    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow() -> str:
        calls.append(1)
        started.set()
        release.wait(5)
        return "schemes"

    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(cache.get_or_compute("k", slow)))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    assert started.wait(5)
    time.sleep(0.05)  # the other threads reach the key and wait for the first one
    release.set()
    for thread in threads:
        thread.join(5)
    assert results == ["schemes"] * 8
    assert calls == [1]


def test_failed_computation_is_not_stored():
    cache = ReadCache()

    def broken() -> str:
        raise RuntimeError("database gone")

    with pytest.raises(RuntimeError):
        cache.get_or_compute("k", broken)
    assert cache.get_or_compute("k", lambda: "ok") == "ok"


def test_expired_entries_are_dropped_on_a_later_store():
    clock = Clock()
    cache = ReadCache(ttl_seconds=30, clock=clock)
    cache.put(("card", "generation-1"), "old card")
    clock.now += 31
    cache.put(("card", "generation-2"), "new card")
    # The replaced publication is not kept referenced until the entry bound pushes it out.
    assert len(cache) == 1
    assert cache.get(("card", "generation-1")) == (False, None)
    assert cache.get(("card", "generation-2")) == (True, "new card")


def test_entries_are_bounded_oldest_first():
    cache = ReadCache(max_entries=2)
    for key in ("a", "b"):
        cache.get_or_compute(key, lambda key=key: key)
    cache.get_or_compute("a", lambda: "stale")  # a hit: "a" becomes the newest
    cache.get_or_compute("c", lambda: "c")
    assert len(cache) == 2
    assert cache.get_or_compute("a", lambda: "recomputed") == "a"
    assert cache.get_or_compute("b", lambda: "recomputed") == "recomputed"

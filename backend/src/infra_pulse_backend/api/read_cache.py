"""Process-local cache of forecast reads bound to one publication (OPS-01, 28.09.2026).

Forecast data (cards, outcomes, window events, layout) change only in a recompute, which
publishes ``generation + 1`` in one transaction (``operations.forecast_publish``). A read
keys its cached part by the publication its own transaction sees (``forecast_db``:
database, scope, generation, ``data_as_of`` and the run's ``created_at``), so the first
read after a new publication misses and recomputes. ``TTL_SECONDS`` also bounds what may
change without a publication: late source records in scheme alarms. Dispatcher decisions
and check results are never cached; the journal reads them on every request.

One computation per key at a time: concurrent requests wait for it instead of repeating
it (single flight). Values are shared between requests and are never mutated. Memory is
bounded by ``max_entries`` (least recently used first) and expired entries are dropped
at least every TTL, so values of a replaced publication do not stay referenced.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import TypeVar

T = TypeVar("T")

TTL_SECONDS = 30.0
MAX_ENTRIES = 256


class ReadCache:
    def __init__(
        self,
        *,
        ttl_seconds: float = TTL_SECONDS,
        max_entries: int = MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[Hashable, tuple[float, object]] = OrderedDict()
        self._flights: dict[Hashable, threading.Lock] = {}
        self._next_purge = clock() + ttl_seconds

    def _fresh(self, key: Hashable) -> tuple[bool, object]:
        entry = self._entries.get(key)
        if entry is None:
            return False, None
        if entry[0] <= self._clock():
            del self._entries[key]
            return False, None
        self._entries.move_to_end(key)
        return True, entry[1]

    def _store(self, key: Hashable, value: object) -> None:
        now = self._clock()
        if now >= self._next_purge:
            for stale in [name for name, (expires, _) in self._entries.items() if expires <= now]:
                del self._entries[stale]
            self._next_purge = now + self._ttl
        self._entries[key] = (now + self._ttl, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def get(self, key: Hashable) -> tuple[bool, object]:
        """``(True, value)`` for a fresh entry, else ``(False, None)``."""
        with self._lock:
            return self._fresh(key)

    def put(self, key: Hashable, value: object) -> None:
        with self._lock:
            self._store(key, value)

    def get_or_compute(self, key: Hashable | None, compute: Callable[[], T]) -> T:
        """Cached value of ``key``; ``None`` (no publication to key by) computes uncached."""
        if key is None:
            return compute()
        with self._lock:
            found, value = self._fresh(key)
            if found:
                return value  # type: ignore[return-value]
            flight = self._flights.setdefault(key, threading.Lock())
        with flight:
            with self._lock:
                found, value = self._fresh(key)
                if found:
                    return value  # type: ignore[return-value]
            try:
                value = compute()
                with self._lock:
                    self._store(key, value)
            finally:
                with self._lock:
                    if self._flights.get(key) is flight:
                        del self._flights[key]
        return value  # type: ignore[return-value]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

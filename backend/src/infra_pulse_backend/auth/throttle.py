"""Failed-login throttle (SEC-02): sliding window per username and per client address.

In-process state: the API runs as one uvicorn process on the stand, and a restart only
resets the counters. A blocked attempt never reaches the directory. A successful login
clears the username counter, not the address counter.
"""

import time
from collections import deque
from collections.abc import Callable
from threading import Lock

ADDRESS_FACTOR = 4
# Expired counters are dropped lazily; a full sweep runs once this many keys exist.
_PRUNE_AT = 10_000


class LoginThrottle:
    def __init__(
        self,
        max_failures: int,
        window_seconds: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.window = float(window_seconds)
        self._clock = clock
        self._failures: dict[tuple[str, str], deque[float]] = {}
        self._lock = Lock()

    def _keys(self, username: str, address: str | None) -> list[tuple[tuple[str, str], int]]:
        keys = [(("user", username.lower()), self.max_failures)]
        if address:
            keys.append((("addr", address), self.max_failures * ADDRESS_FACTOR))
        return keys

    def _recent(self, key: tuple[str, str], now: float) -> deque[float]:
        attempts = self._failures.get(key)
        if attempts is None:
            return deque()
        while attempts and attempts[0] <= now - self.window:
            attempts.popleft()
        if not attempts:
            del self._failures[key]
        return attempts

    def retry_after(self, username: str, address: str | None) -> int:
        """Seconds until another attempt is allowed; 0 when allowed now."""
        now = self._clock()
        with self._lock:
            wait = 0.0
            for key, limit in self._keys(username, address):
                attempts = self._recent(key, now)
                if len(attempts) >= limit:
                    wait = max(wait, attempts[len(attempts) - limit] + self.window - now)
        return int(wait) + 1 if wait > 0 else 0

    def failure(self, username: str, address: str | None) -> None:
        now = self._clock()
        with self._lock:
            if len(self._failures) >= _PRUNE_AT:
                for key in list(self._failures):
                    self._recent(key, now)
            for key, _ in self._keys(username, address):
                self._failures.setdefault(key, deque()).append(now)

    def success(self, username: str) -> None:
        with self._lock:
            self._failures.pop(("user", username.lower()), None)

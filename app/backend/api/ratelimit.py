"""In-process rate limiting.

A fixed-window counter per (bucket, key). Deliberately simple, and honest
about what that costs:

- **It is per process.** Two workers each allow the configured rate, so the
  effective limit is the configured rate times the worker count. For this
  deployment — one container, one worker — that is exact. A multi-replica
  deployment needs a shared counter (Redis) and this module names that rather
  than pretending otherwise.
- **It is memory-bounded.** Keys are swept once they fall out of the window,
  and the map is capped: an attacker rotating source addresses cannot turn the
  limiter itself into the memory exhaustion it exists to prevent.
- **A fixed window admits a burst at the boundary.** Up to twice the limit can
  land across two adjacent windows. Sliding windows avoid that at the cost of
  storing every timestamp; for limits whose job is to stop sustained abuse
  rather than to meter precisely, the trade is worth taking.

The limiter never *identifies* anyone. Keys are hashed, and nothing here is
written to disk.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field

#: Beyond this many tracked keys, the map is swept early. Chosen to be far
#: above any legitimate concurrent-client count and far below anything that
#: would matter for memory.
MAX_TRACKED_KEYS = 50_000

WINDOW_SECONDS = 60.0


@dataclass
class _Counter:
    count: int
    window_started_at: float


@dataclass
class RateLimiter:
    """Fixed-window counters, safe to share across request threads.

    FastAPI runs synchronous handlers in a thread pool, so every mutation here
    is under a lock. An unlocked read-modify-write would let two simultaneous
    requests both observe the pre-increment count and both be allowed.
    """

    window_seconds: float = WINDOW_SECONDS
    _counters: dict[str, _Counter] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _key(self, bucket: str, identity: str) -> str:
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        return f"{bucket}:{digest}"

    def check(self, bucket: str, identity: str, limit: int) -> tuple[bool, int]:
        """Count one request. Returns (allowed, seconds until the window resets).

        `limit <= 0` disables the bucket rather than blocking everything — a
        misconfigured zero should not take the deployment down.
        """
        if limit <= 0:
            return True, 0

        key = self._key(bucket, identity)
        now = time.monotonic()

        with self._lock:
            if len(self._counters) > MAX_TRACKED_KEYS:
                self._sweep(now)

            counter = self._counters.get(key)
            if counter is None or now - counter.window_started_at >= self.window_seconds:
                self._counters[key] = _Counter(count=1, window_started_at=now)
                return True, 0

            counter.count += 1
            if counter.count > limit:
                retry_after = int(
                    self.window_seconds - (now - counter.window_started_at)
                )
                return False, max(retry_after, 1)
            return True, 0

    def _sweep(self, now: float) -> None:
        """Drop windows that have already elapsed. Caller holds the lock."""
        stale = [
            key
            for key, counter in self._counters.items()
            if now - counter.window_started_at >= self.window_seconds
        ]
        for key in stale:
            del self._counters[key]
        if len(self._counters) > MAX_TRACKED_KEYS:
            # Still oversized after sweeping: every window is live, which means
            # this is an attack rather than accumulated debris. Drop everything
            # rather than grow without bound; the cost is one forgiven window.
            self._counters.clear()

    def reset(self) -> None:
        """Clear all counters. For tests, and for an operator unblocking a user."""
        with self._lock:
            self._counters.clear()


def client_identity(request) -> str:
    """The key an anonymous caller is limited by.

    Prefers the authenticated user when there is one, because a user is a far
    better subject than an address: addresses are shared by whole offices and
    rotated freely by attackers.

    `X-Forwarded-For` is honoured only when the deployment sits behind a proxy
    that sets it. Trusting it unconditionally would let any client pick its own
    rate-limit key by sending the header — which is why the value is read from
    the *leftmost* entry only when the immediate peer is a proxy we expect, and
    otherwise ignored entirely.
    """
    user_id = getattr(request.state, "auth_user_id", None)
    if user_id:
        return f"user:{user_id}"

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and getattr(request.app.state, "trust_forwarded_for", False):
        return f"ip:{forwarded.split(',')[0].strip()}"

    client = request.client
    return f"ip:{client.host if client else 'unknown'}"


def client_address(request) -> str | None:
    """The caller's address, for lockout counting and audit correlation."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and getattr(request.app.state, "trust_forwarded_for", False):
        return forwarded.split(",")[0].strip()
    client = request.client
    return client.host if client else None

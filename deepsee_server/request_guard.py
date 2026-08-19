"""Process-local rate and concurrency protection for inference requests."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int):
        super().__init__("rate limit exceeded")
        self.retry_after = retry_after


class QueueTimeout(Exception):
    """Raised when no concurrency slot becomes available in time."""


@dataclass
class GuardLease:
    _semaphore: asyncio.Semaphore
    _released: bool = False

    async def release(self) -> None:
        if not self._released:
            self._released = True
            self._semaphore.release()

    async def __aenter__(self) -> "GuardLease":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.release()


class RequestGuard:
    def __init__(
        self,
        *,
        max_concurrent: int,
        queue_timeout: float,
        rate_limit: int,
        rate_window: float,
    ) -> None:
        values = (max_concurrent, queue_timeout, rate_limit, rate_window)
        if (
            isinstance(max_concurrent, bool)
            or isinstance(rate_limit, bool)
            or max_concurrent <= 0
            or rate_limit <= 0
            or queue_timeout <= 0
            or rate_window <= 0
            or not all(math.isfinite(float(value)) for value in values)
        ):
            raise ValueError("request guard settings must be positive and finite")
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._queue_timeout = float(queue_timeout)
        self._rate_limit = rate_limit
        self._rate_window = float(rate_window)
        self._rate_lock = asyncio.Lock()
        self._windows: dict[str, tuple[float, int]] = {}

    async def acquire(self, identity: str) -> GuardLease:
        now = time.monotonic()
        async with self._rate_lock:
            started, count = self._windows.get(identity, (now, 0))
            if now - started >= self._rate_window:
                started, count = now, 0
            if count >= self._rate_limit:
                retry_after = max(
                    1, math.ceil(self._rate_window - (now - started))
                )
                raise RateLimitExceeded(retry_after)
            self._windows[identity] = (started, count + 1)

        try:
            await asyncio.wait_for(
                self._semaphore.acquire(), timeout=self._queue_timeout
            )
        except asyncio.TimeoutError as exc:
            raise QueueTimeout("request queue timeout") from exc
        return GuardLease(self._semaphore)

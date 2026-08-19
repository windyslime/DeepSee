"""Bounded metadata-only request traces for the local admin UI."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from threading import Lock
import time


@dataclass(frozen=True)
class RequestTrace:
    id: str
    method: str
    path: str
    status: int
    latency_ms: int
    created_at: int = 0
    route: str | None = None
    has_image: bool = False
    image_count: int = 0
    cache_hits: int = 0
    upstream_model: str | None = None
    error_type: str | None = None
    vision_analysis: str | None = None


class TraceStore:
    def __init__(self, max_entries: int = 200):
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._items: deque[RequestTrace] = deque(maxlen=max_entries)
        self._lock = Lock()

    def append(self, trace: RequestTrace) -> None:
        if trace.created_at == 0:
            trace = RequestTrace(**{**asdict(trace), "created_at": int(time.time())})
        with self._lock:
            self._items.append(trace)

    def list(self) -> list[dict]:
        with self._lock:
            return [asdict(item) for item in reversed(self._items)]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


request_traces = TraceStore()

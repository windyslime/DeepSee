"""Opt-in gateway restart control for supervised local deployments."""

from __future__ import annotations

import os
import signal
import threading
from collections.abc import Callable, Mapping

Schedule = Callable[[float, Callable[[], None]], object]


def _schedule(delay: float, callback: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()
    return timer


def _terminate() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


class RestartController:
    """Hide restart capability checks and one-shot termination scheduling."""

    def __init__(
        self,
        *,
        enabled: bool,
        environment: Mapping[str, str] | None = None,
        schedule: Schedule = _schedule,
        terminate: Callable[[], None] = _terminate,
        delay: float = 0.15,
    ) -> None:
        self._enabled = enabled
        self._environment = dict(os.environ) if environment is None else dict(environment)
        self._schedule = schedule
        self._terminate = terminate
        self._delay = delay
        self._lock = threading.Lock()
        self._scheduled = False

    @property
    def supported(self) -> bool:
        return self._enabled and bool(self._environment.get("XPC_SERVICE_NAME"))

    def request_restart(self) -> bool:
        if not self.supported:
            return False
        with self._lock:
            if self._scheduled:
                return True
            self._scheduled = True
            self._schedule(self._delay, self._terminate)
        return True


_restart_controller = RestartController(enabled=False)


def configure_restart_controller(controller: RestartController) -> None:
    global _restart_controller
    _restart_controller = controller


def configured_restart_controller() -> RestartController:
    return _restart_controller

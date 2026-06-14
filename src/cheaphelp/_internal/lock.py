"""File-based exclusive locks for the orchestrator.

Uses `fcntl.flock` on a file under the workspace so the kernel releases the
lock automatically when the holding process exits — a SIGKILL of the holder
cannot wedge future runs.

Two modes:

- non-blocking (default): `acquired` is False on contention, so the caller can
  skip the locked work and move on (per-issue locks let concurrent ticks work
  on *different* issues without colliding).
- blocking: wait until the lock is free, then acquire it (used to serialise the
  shared read-only clone's fetch/reset across overlapping ticks).
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path


class RunLock:
    """Exclusive `fcntl` lock on a file, usable as a context manager.

    After `__enter__`, `acquired` is True iff this process now holds the lock.
    With `blocking=False` (the default) contention leaves `acquired` False so the
    caller can skip; with `blocking=True` `__enter__` waits until the lock is
    free and `acquired` is always True (barring an `os.open` failure).
    """

    def __init__(self, path: Path, *, blocking: bool = False) -> None:
        self.path = path
        self.blocking = blocking
        self.fd: int | None = None
        self.acquired: bool = False

    # Return the concrete class (not `typing.Self`, which is 3.11+) so the module
    # imports on the project's py310 minimum.
    def __enter__(self) -> RunLock:
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return self
        flags = fcntl.LOCK_EX if self.blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(self.fd, flags)
        except OSError:  # BlockingIOError (NB contention) or other lock failure
            os.close(self.fd)
            self.fd = None
            return self
        try:
            os.ftruncate(self.fd, 0)
            os.write(self.fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass  # the lock is what matters; PID is informational
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        if self.fd is not None:
            if self.acquired:
                with contextlib.suppress(OSError):
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None
        self.acquired = False

    @property
    def holder_pid(self) -> int | None:
        """Return the PID of the current lock holder, or None if unavailable."""
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not text:
            return None
        try:
            return int(text.splitlines()[0])
        except ValueError:
            return None

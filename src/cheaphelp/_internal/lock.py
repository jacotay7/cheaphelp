"""Workspace-level exclusive lock to serialise orchestrator ticks.

Uses `fcntl.flock(LOCK_EX | LOCK_NB)` on a file under the workspace so the
kernel releases the lock automatically when the holding process exits — a
SIGKILL of the holder cannot wedge future runs.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path


class RunLock:
    """Exclusive non-blocking lock on a workspace's `run.lock` file.

    Use as a context manager. After `__enter__`, `acquired` is True iff this
    process now holds the lock; on contention it is False and the existing
    tick should skip and exit 0.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None
        self.acquired: bool = False

    # Return the concrete class (not `typing.Self`, which is 3.11+) so the module
    # imports on the project's py310 minimum.
    def __enter__(self) -> RunLock:
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return self
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
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

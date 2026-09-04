"""Small cross-process exclusive file lock implemented with the stdlib."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import TracebackType


_THREAD_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


def _thread_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _THREAD_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


class ExclusiveFileLock:
    """Advisory one-byte lock with a bounded acquisition timeout."""

    def __init__(self, path: Path, timeout: float = 15.0, poll: float = 0.025):
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self._file = None
        self._thread_lock = _thread_lock(path)

    def __enter__(self) -> "ExclusiveFileLock":
        deadline = time.monotonic() + self.timeout
        remaining = max(0.0, deadline - time.monotonic())
        if not self._thread_lock.acquire(timeout=remaining):
            raise TimeoutError(f"timed out acquiring lock: {self.path}")

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("a+b")
            if self._file.seek(0, os.SEEK_END) == 0:
                self._file.write(b"\0")
                self._file.flush()
                os.fsync(self._file.fileno())

            while True:
                try:
                    self._lock_os()
                    return self
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out acquiring lock: {self.path}")
                    time.sleep(self.poll)
        except BaseException:
            if self._file is not None:
                self._file.close()
                self._file = None
            self._thread_lock.release()
            raise

    def _lock_os(self) -> None:
        assert self._file is not None
        self._file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_os(self) -> None:
        assert self._file is not None
        self._file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if self._file is not None:
                self._unlock_os()
                self._file.close()
                self._file = None
        finally:
            self._thread_lock.release()

"""File locks coordinating case publication across local rg2watch processes.

Lock order is always publish then decision.  Publish locks are non-blocking for user work;
decision locks are short blocking locks around the verdict/share boundary.  ``flock`` releases
both locks when a process exits.  These locks require all processes to share one local filesystem.
"""
import fcntl
import hashlib
import os
import threading
from pathlib import Path


_memory_guard = threading.Lock()
_memory_locks: dict[tuple[str, str], threading.Lock] = {}


class _ThreadLock:
    def __init__(self, lock: threading.Lock):
        self.lock = lock

    def close(self):
        self.lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _FileLock:
    def __init__(self, fh):
        self.fh = fh

    def close(self):
        if self.fh is not None:
            fh, self.fh = self.fh, None
            fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class CaseLocks:
    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)

    def _take(self, url: str, kind: str, blocking: bool):
        digest = hashlib.sha256(url.encode()).hexdigest()
        if self.db_path == ":memory:":
            key = (kind, digest)
            with _memory_guard:
                lock = _memory_locks.setdefault(key, threading.Lock())
            if not lock.acquire(blocking=blocking):
                return None
            return _ThreadLock(lock)
        db = Path(self.db_path).resolve()
        path = db.parent / f".{db.name}.{digest}.{kind}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        fh = os.fdopen(fd, "a+")
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fh, flags)
        except BlockingIOError:
            fh.close()
            return None
        return _FileLock(fh)

    def publish(self, url: str, blocking: bool = False):
        return self._take(url, "publish", blocking)

    def decision(self, url: str):
        return self._take(url, "decision", True)

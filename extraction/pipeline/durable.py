"""Crash-safe file primitives: an atomic replace that is fsynced before it is
renamed into place, and a non-blocking exclusive lock across processes."""
from __future__ import annotations

import os
from pathlib import Path


class LockHeld(RuntimeError):
    """Another process holds the submission lock."""


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class FileLock:
    def __init__(self, path: Path):
        self._path = path
        self._fh = None

    def __enter__(self) -> FileLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._path, "a+b")
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise LockHeld(f"another batch command holds {self._path}") from exc
        self._fh = fh
        return self

    def __exit__(self, *exc_info) -> None:
        fh = self._fh
        self._fh = None
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()

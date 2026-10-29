from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path


def now() -> str:
    return datetime.now(UTC).isoformat()


def safe_error(exc: BaseException) -> str:
    # Only our controlled error codes reach artifacts; provider bodies/DSNs may contain secrets.
    text = str(exc)
    return text if re.fullmatch(r"[A-Z][A-Z0-9_]*(?::[0-9]+)?", text) else type(exc).__name__


class Lease:
    """OS-owned advisory lease: released automatically if the reviewing process dies."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError("REVIEW_ALREADY_RUNNING") from None

    def close(self):
        if self.handle.closed:
            return
        self.handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()

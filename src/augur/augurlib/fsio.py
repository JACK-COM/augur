"""Writing Augur's files: atomic, durable replacement and the lock held across a manifest's
read-modify-write.
"""
import contextlib
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path

from . import errors
from . import paths


def _write_json(path, obj, indent=2):
    """Write whole or not at all. A temporary file of its own beside the target, flushed to disk,
    is renamed over it, so neither a reader nor a second writer sees half a file; the target keeps
    its mode, and a symlinked augur.json keeps its link, the file it points at being replaced."""
    target = Path(os.path.realpath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = target.stat().st_mode & 0o777
    except FileNotFoundError:
        mask = os.umask(0)
        os.umask(mask)
        mode = 0o666 & ~mask                # what a plain open() would have made
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    try:
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with f:
            f.write(json.dumps(obj, indent=indent, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
            if hasattr(fcntl, "F_FULLFSYNC"):   # macOS: fsync alone leaves it in the drive's cache
                try:
                    fcntl.fcntl(f.fileno(), fcntl.F_FULLFSYNC)
                except OSError:
                    pass
        os.chmod(tmp, mode)                 # mkstemp makes 0600
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:                                    # the rename itself, durable across a crash
        d = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(d)
        finally:
            os.close(d)
    except OSError:
        pass                                # a filesystem that cannot sync a directory still renamed
    return Path(path)


LOCK_WAIT = 10


@contextlib.contextmanager
def _manifest_lock():
    """Hold augur.json's lock across a read-modify-write, so two `configure` runs at once cannot
    each read the old file and the second write drop the first one's change. A holder only reads
    and writes a small file, so one still holding it after LOCK_WAIT seconds is stuck, and the
    error names the lock to remove."""
    lock = paths.home() / "augur.json.lock"
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # never through a link: opening a planted one for writing would truncate its target
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with open(fd, "w") as f:
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise errors.AugurUnavailable(f"{lock} is held by another augur for over {LOCK_WAIT}s; "
                                           f"if none is running, remove it")
                time.sleep(0.05)
        yield                               # closing the file releases the lock

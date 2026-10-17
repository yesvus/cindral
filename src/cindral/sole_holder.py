"""One broker per queue, enforced rather than assumed.

The status ordering in the queue is guarded by a lock that lives in the process
that holds the queue, so a second broker pointed at the same database would post
against a row it is not the owner of and could report a run with a verdict that
has already been superseded. The design has always been one active broker; this
is what makes a second one fail to start instead of quietly doubling up.
"""
import errno
import fcntl
import os
from pathlib import Path
from typing import IO


class QueueHeld(RuntimeError):
    """Another process is already the broker for this queue."""


def take(path: str) -> IO[bytes]:
    """Take exclusive hold of a queue, or refuse.

    Held for the life of the file handle, so the hold ends when the process does
    and not before, which is what `flock` on an open descriptor already means. A
    stale lock from a killed process disappears with it rather than needing a
    sweep, and a lock file left behind by one is reused rather than mistaken for
    a second queue.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = open(target, "a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno not in (errno.EACCES, errno.EAGAIN):
            # a filesystem with no working flock, or a permission problem. Saying
            # another broker holds the queue would be a false diagnosis of
            # something that is not that at all
            raise
        raise QueueHeld(
            f"another broker already holds {target}. Two brokers on one queue would"
            " report runs against verdicts that have been superseded, so this one"
            " is not starting."
        ) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n".encode())
    handle.flush()
    return handle

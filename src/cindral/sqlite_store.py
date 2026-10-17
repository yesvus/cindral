"""Open a broker database the way every store needs one, and wait out the race.

Five stores each opened SQLite the same four-line way, and the journal-mode
pragma among those lines is the one that loses: switching a database to WAL
takes a brief exclusive lock, and a connection that finds another process inside
that window is refused with "database is locked" rather than made to wait, which
the connection's own busy timeout does not reliably cover. So a second broker
starting, or any two touching the same file, could fail at open time on a
database that was perfectly fine.

The wait is bounded, because a lock that never clears is a refusal with a cause
rather than a hang.
"""
import sqlite3
import time
from typing import Any

__all__ = ["WAL_WAIT_SECONDS", "open_database"]

#: the ceiling on waiting out a contended journal mode. A broker that cannot get
#: the lock in this long has a real problem, and saying so beats waiting for ever.
WAL_WAIT_SECONDS = 10.0


def open_database(
    path: str,
    timeout: float = 5.0,
    isolation_level: Any = None,
    wait_seconds: float = WAL_WAIT_SECONDS,
) -> sqlite3.Connection:
    """Connect with the row factory, the journal mode and the busy timeout set.

    `isolation_level` is the caller's business: passing None gives the caller
    explicit transactions and a default leaves sqlite3 managing them, and the
    stores that rely on that difference must keep it.
    """
    connection = sqlite3.connect(path, timeout=timeout, isolation_level=isolation_level)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        _enable_wal(connection, wait_seconds)
        return connection
    except BaseException:
        connection.close()
        raise


def _enable_wal(connection: sqlite3.Connection, wait_seconds: float) -> None:
    """Put the database in WAL, waiting out a connection that is mid-switch.

    Only a locked or busy database is waited for. Any other error is a real fault
    and is raised, and a rollback is issued first because a refused pragma can
    leave the connection holding a read it must not keep.

    The per-attempt wait is trimmed to what is left of the budget rather than left
    at the connection's own timeout. A refused pragma blocks for the whole of
    `busy_timeout` before it returns, so leaving it at five seconds means the last
    attempt can start just under the deadline and then overshoot it by five more,
    and the ceiling this function documents would be a fiction. Trimming it is what
    makes the deadline a deadline.
    """
    deadline = time.monotonic() + wait_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _wal_refused(wait_seconds)
        connection.execute(f"PRAGMA busy_timeout={max(1, int(remaining * 1000))}")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            return
        except sqlite3.OperationalError as exc:
            reason = str(exc).lower()
            if "locked" not in reason and "busy" not in reason:
                raise
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            if time.monotonic() >= deadline:
                # one message for both ways the budget runs out, so a startup log
                # says the same thing whichever attempt was in flight
                raise _wal_refused(wait_seconds) from exc
            time.sleep(0.05)


def _wal_refused(wait_seconds: float) -> sqlite3.OperationalError:
    """The refusal a lock that never cleared turns into.

    Named rather than re-raised so the message says what the caller waited for,
    which is the part an operator reading a startup log cannot otherwise see: that
    the database was reachable and another process held the journal-mode switch for
    the whole budget.
    """
    return sqlite3.OperationalError(
        f"another process held the journal mode for {wait_seconds:.0f}s, so WAL was not enabled"
    )

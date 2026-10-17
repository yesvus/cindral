"""What a verdict owes the commit, and when it stops owing it.

Kept apart from the reclaim because this is the bookkeeping and the reclaim is
the event. The sequence number that travels with each verdict lives here too: it
is what says whether a verdict still in hand is the run's latest state. Every
read and write of an owed status goes through the store's lock, so the two drains
cannot contradict each other.
"""
import sqlite3
from collections.abc import Callable
from typing import Any, Protocol

from .jobrecord import Job, _row


def _owed_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM jobs WHERE pending_status IS NOT NULL ORDER BY created_at"
    ).fetchall()


def owed_statuses(connection: sqlite3.Connection) -> list[tuple[Job, str]]:
    """Runs whose verdict reached the queue but not yet the commit."""
    return [(_row(row), row["pending_status"]) for row in _owed_rows(connection)]


def current_seq(connection: sqlite3.Connection, job_id: str) -> int:
    """How many verdicts this run has been given, published or not.

    The value a post carries, and the one it checks before it goes out. Two
    drains can hold verdicts for the same run at the same time, and without this
    the older one can land last and report a lost run as lost after a later one
    reported it green.
    """
    row = connection.execute("SELECT status_seq FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return int(row["status_seq"]) if row is not None else 0


def claim_owed(connection: sqlite3.Connection) -> list[tuple[Job, str, int]]:
    """Take the verdicts the queue owes, so one drainer posts each exactly once.

    Taken in the caller's write transaction, which is what makes the post
    at-most-once: the value leaves the row before the network call, so a drainer
    that reads after this transaction finds nothing to send. A verdict that
    cannot be posted goes back with `restore_status`.
    """
    taken = [
        (_row(row), row["pending_status"], int(row["status_seq"]))
        for row in _owed_rows(connection)
    ]
    for job, status, _seq in taken:
        discard_status(connection, job.id, status)
    return taken


def discard_status(connection: sqlite3.Connection, job_id: str, status: str) -> None:
    """Drop an owed status only while it is still the one that was read.

    A verdict superseded between the read and the write belongs to a later
    release, and the later text is the run's actual state, so the newer value
    stands and the older one is dropped.
    """
    connection.execute(
        "UPDATE jobs SET pending_status = NULL WHERE id = ? AND pending_status = ?",
        (job_id, status),
    )


def restore_status(
    connection: sqlite3.Connection, job_id: str, status: str, seq: int
) -> None:
    """Put a verdict back when its post failed, unless something newer took it.

    A release that arrived while the post was in flight supersedes the verdict
    this drain took, so the restore is refused rather than overwriting the newer
    text, and the run keeps the status the queue owes for its current state. The
    sequence is part of the test for the same reason: a verdict restored against
    a row that has moved on is owed forever, because every later drain reads it,
    finds it stale, and drops it again.
    """
    connection.execute(
        "UPDATE jobs SET pending_status = ? WHERE id = ? AND pending_status IS NULL"
        " AND status_seq = ?",
        (status, job_id, seq),
    )


def owe_status(connection: sqlite3.Connection, job_id: str, status: str) -> None:
    """Record a verdict the broker could not send, so the next drain sends it.

    Written with the sequence moved on, so a drain that is already holding an
    older verdict for the run learns that its post is stale rather than sending
    the text that failed to go out.
    """
    connection.execute(
        "UPDATE jobs SET pending_status = ?, status_seq = status_seq + 1 WHERE id = ?",
        (status, job_id),
    )


def clear_status(connection: sqlite3.Connection, job_id: str) -> None:
    """Drop an owed status once it has reached the commit, or been superseded."""
    connection.execute("UPDATE jobs SET pending_status = NULL WHERE id = ?", (job_id,))


class JobQueue(Protocol):
    """The store surface a drain drives, so it needs no server and no queue.

    Stated here because the drain is the only thing that has to know both: it
    claims a verdict, checks whether it is still current, and puts it back when
    the post failed. A narrower protocol is what keeps the two drains honest,
    since they are the same code called from two places.
    """

    def reclaim_expired(
        self, now: float | None = ..., publish: Callable[[Job], str] | None = ...
    ) -> list[Job]: ...

    def claim_owed_statuses(self) -> list[tuple[Job, str, int]]: ...

    def restore_status(self, job_id: str, status: str, seq: int) -> None: ...

    def status_seq(self, job_id: str) -> int: ...

    @property
    def status_lock(self) -> Any: ...

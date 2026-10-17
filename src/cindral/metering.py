"""A ledger of dispatches that cost money, written where the money is spent.

A metered dispatch can be issued from three places: the push webhook, a pull
request, and an explicit call to the dispatch endpoint. Only the first of those
had anywhere to leave a record, so a ceiling counted from those records saw
nothing for the other two and never moved. Counting the *spend* rather than the
*request* is what makes the ceiling hold on every path, and it is also the
truthful order of events: a dispatch that failed was not charged, so it is not
recorded here.
"""
from collections.abc import Iterator
from .sqlite_store import open_database
import contextlib
import json
import sqlite3
import time

HOSTED_LANE = "hosted"

#: a hold is taken against the budget, not against a repository, and is given its
#: repository when the dispatch it was for is known
REPOSITORY_UNKNOWN = "-"


class MeteredLedger:
    """One row per metered dispatch, appended at the moment it is accepted."""

    def __init__(self, path: str) -> None:
        self.path = path
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metered_dispatches ("
                "id INTEGER PRIMARY KEY, repository TEXT NOT NULL, lane TEXT NOT NULL,"
                "workflow TEXT NOT NULL, ref TEXT NOT NULL, created_at REAL NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS metered_dispatches_month"
                " ON metered_dispatches(lane, created_at)"
            )

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = open_database(self.path, timeout=5, isolation_level="")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def reserve(
        self,
        repository: str,
        lane: str,
        workflow: str = "",
        ref: str = "",
        now: float | None = None,
    ) -> int:
        """Hold the cost of one dispatch, and return the hold's id.

        Recorded before the dispatch is attempted, not after it succeeds, because
        a ceiling that is checked and then written is a ceiling that two
        simultaneous requests both pass: they read the same spend, and neither
        has written yet. The hold is what makes the read and the write one step.
        """
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO metered_dispatches (repository, lane, workflow, ref, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (repository.lower(), lane, workflow, ref, time.time() if now is None else now),
            )
        return int(cursor.lastrowid or 0)

    def attribute(
        self, reservation: int | None, repository: str, workflow: str = "", ref: str = ""
    ) -> None:
        """Name the repository and workflow a hold turned out to be for.

        The hold is taken before either is known, because the ceiling is checked
        before routing decides anything. Leaving it unnamed would make the month
        unreadable to whoever has to reconcile a bill against it.
        """
        if reservation is None:
            return
        with self._connect() as connection:
            connection.execute(
                "UPDATE metered_dispatches SET repository = ?, workflow = ?, ref = ? WHERE id = ?",
                (repository.lower(), workflow, ref, reservation),
            )

    def release(self, reservation: int | None) -> None:
        """Give a hold back, because the dispatch it was taken for did not go.

        A dispatch GitHub refused cost nothing, so counting it would make the
        ceiling tighter than the money that was actually spent.
        """
        if reservation is None:
            return
        with self._connect() as connection:
            connection.execute("DELETE FROM metered_dispatches WHERE id = ?", (reservation,))

    def count(self, since: float, lane: str = HOSTED_LANE) -> int:
        """How many dispatches on this lane are held or accepted since an instant."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM metered_dispatches WHERE lane = ? AND created_at >= ?",
                (lane, since),
            ).fetchone()
        return int(row["n"]) if row else 0

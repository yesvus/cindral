"""Persist signed webhook dispatches for the single active broker."""
from dataclasses import dataclass
from contextlib import contextmanager
import json
import sqlite3
import time
from typing import Iterator

from .retry import MAX_ERROR_CHARS
from .sqlite_store import open_database


#: how long a broker waits out a locked ledger before refusing to start.
#:
#: The same number as the journal-mode pragma's budget in sqlite_store, and the
#: difference is not the number. The pragma trims its per-statement wait to what is
#: left of its budget, so it cannot overshoot; this loop can, by one five-second
#: statement. Both are bounded, and only one of them bounds itself.
MIGRATION_LOCK_WAIT_SECONDS = 10.0

READY = "ready"
ATTEMPTING = "attempting"
ACCEPTED = "accepted"
REJECTED = "rejected"
UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class Delivery:
    repository: str
    delivery: str
    fingerprint: str
    workflow: str
    ref: str
    inputs: dict[str, str]
    state: str
    # the pushed commit, so a delivery replayed after a restart can be handed
    # to the device pool, which checks a commit out and has no payload to read
    # one from. Empty on a row registered before this column existed.
    sha: str = ""


class DeliveryStore:
    """One automatic GitHub dispatch attempt per repository and delivery ID."""

    def __init__(self, path: str) -> None:
        self.path = path
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS webhook_deliveries ("
                "repository TEXT NOT NULL, delivery TEXT NOT NULL, fingerprint TEXT NOT NULL,"
                "workflow TEXT NOT NULL, ref TEXT NOT NULL, inputs TEXT NOT NULL,"
                "state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,"
                "error TEXT, sha TEXT NOT NULL DEFAULT '', PRIMARY KEY (repository, delivery))"
            )
            self._migrate(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS webhook_deliveries_state ON webhook_deliveries(state, created_at)"
            )

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Add the commit column to a ledger written before it carried one.

        A row that predates it holds no commit, so a replay of that row can
        report a refusal but cannot queue the push anywhere. SQLite cannot add a
        NOT NULL column to a table that has rows without a default, so the
        empty default is what the migration leaves behind.

        The read that decides and the write that migrates are not one atomic step,
        so two brokers opening the same pre-migration ledger both see the column
        missing and both try to add it. That is the documented deployment, and the
        two failures it produces are not the same thing and must not be treated as
        one. A duplicate column means the other broker won, which is a successful
        outcome here: the next read proves the column is there. A locked database
        says nothing at all about the schema, and the column may still be missing,
        so it is waited out and read again rather than resolved by re-reading once
        and giving up. Conflating them turned a transient lock into a broker that
        would not start, on a ledger that was perfectly fine.
        """
        deadline = time.monotonic() + MIGRATION_LOCK_WAIT_SECONDS
        while True:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(webhook_deliveries)")}
            if "sha" in columns:
                return
            try:
                connection.execute("ALTER TABLE webhook_deliveries ADD COLUMN sha TEXT NOT NULL DEFAULT ''")
                return
            except sqlite3.OperationalError as exc:
                reason = str(exc).lower()
                if "duplicate column" in reason:
                    continue
                if "locked" in reason or "busy" in reason:
                    if time.monotonic() >= deadline:
                        raise
                    try:
                        connection.rollback()
                    except sqlite3.Error:
                        pass
                    time.sleep(0.05)
                    continue
                raise

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = open_database(self.path, timeout=5, isolation_level="")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _delivery(row: sqlite3.Row) -> Delivery:
        return Delivery(
            row["repository"], row["delivery"], row["fingerprint"],
            row["workflow"], row["ref"], json.loads(row["inputs"]), row["state"], row["sha"],
        )

    def get(self, repository: str, delivery: str) -> Delivery | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM webhook_deliveries WHERE repository = ? AND delivery = ?",
                (repository.lower(), delivery),
            ).fetchone()
            return self._delivery(row) if row else None

    def register(
        self, repository: str, delivery: str, fingerprint: str,
        workflow: str, ref: str, inputs: dict[str, str], sha: str = "",
    ) -> Delivery:
        repository = repository.lower()
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO webhook_deliveries "
                "(repository, delivery, fingerprint, workflow, ref, inputs, state, created_at, updated_at, sha) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (repository, delivery, fingerprint, workflow, ref, json.dumps(inputs), READY, now, now, sha),
            )
            row = connection.execute(
                "SELECT * FROM webhook_deliveries WHERE repository = ? AND delivery = ?",
                (repository, delivery),
            ).fetchone()
            assert row is not None
            return self._delivery(row)

    def claim(self, repository: str, delivery: str) -> bool:
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE webhook_deliveries SET state = ?, updated_at = ? "
                "WHERE repository = ? AND delivery = ? AND state = ?",
                (ATTEMPTING, time.time(), repository.lower(), delivery, READY),
            ).rowcount
            return changed == 1

    def finish(self, repository: str, delivery: str, state: str, error: str = "") -> None:
        if state not in {ACCEPTED, REJECTED, UNCERTAIN}:
            raise ValueError("invalid delivery result")
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE webhook_deliveries SET state = ?, error = ?, updated_at = ? "
                "WHERE repository = ? AND delivery = ? AND state = ?",
                (state, error[:MAX_ERROR_CHARS], time.time(), repository.lower(), delivery, ATTEMPTING),
            ).rowcount
            if changed != 1:
                raise RuntimeError("delivery attempt changed before its result was recorded")

    def hosted_dispatches_since(self, since: float) -> list[str]:
        """The stored inputs of every delivery created at or after an instant.

        The lane is left to the caller to read out of the inputs, so this stays
        one index-friendly scan and the budget can count any lane rather than
        only the one it was written for.
        """
        with self._connect() as connection:
            return [
                row["inputs"]
                for row in connection.execute(
                    "SELECT inputs FROM webhook_deliveries WHERE created_at >= ?", (since,)
                )
            ]

    def pending(self, limit: int = 30) -> list[Delivery]:
        with self._connect() as connection:
            return [self._delivery(row) for row in connection.execute(
                "SELECT * FROM webhook_deliveries WHERE state = ? ORDER BY created_at LIMIT ?", (READY, limit),
            )]

    def reject_ready(self, repository: str, delivery: str, reason: str) -> bool:
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE webhook_deliveries SET state = ?, error = ?, updated_at = ? "
                "WHERE repository = ? AND delivery = ? AND state = ?",
                (REJECTED, reason[:MAX_ERROR_CHARS], time.time(), repository.lower(), delivery, READY),
            ).rowcount
            return changed == 1

    def recover(self) -> int:
        with self._connect() as connection:
            return connection.execute(
                "UPDATE webhook_deliveries SET state = ?, error = 'broker stopped during dispatch', "
                "updated_at = ? WHERE state = ?",
                (UNCERTAIN, time.time(), ATTEMPTING),
            ).rowcount

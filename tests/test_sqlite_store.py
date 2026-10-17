"""Opening a database has to survive another connection being inside the switch.

The journal-mode pragma needs a brief exclusive lock, and a connection that finds
another process there is refused with "database is locked" rather than made to
wait, so a second broker starting could fail at open time on a file that was
perfectly fine. The busy timeout does not cover it, which is why the wait is
explicit and bounded.

The lock is injected rather than raced for: holding a real exclusive lock long
enough to reproduce this is a test of how many seconds a loaded machine took.
"""
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from cindral import sqlite_store
from cindral.sqlite_store import WAL_WAIT_SECONDS, open_database


def locked_on_journal_mode(real, remaining):
    """A connection whose journal-mode pragma is refused `remaining` times.

    The pragma is the only statement refused, so anything else the opener does
    behaves normally and the test is about the one that races.
    """

    class Locked:
        row_factory = None

        def execute(self, statement, *args):
            if "journal_mode" in statement and remaining[0] > 0:
                remaining[0] -= 1
                raise sqlite3.OperationalError("database is locked")
            return real.execute(statement, *args)

        def rollback(self):
            return real.rollback()

        def close(self):
            return real.close()

    return Locked()


def _level(store) -> object:
    """The isolation level of one connection the store itself opens."""
    opened = store._connect()
    if hasattr(opened, "__enter__"):
        with opened as connection:
            return connection.isolation_level
    try:
        return opened.isolation_level
    finally:
        opened.close()


class OpenDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / "state.db")

    def _open_with(self, times, wait_seconds=WAL_WAIT_SECONDS):
        remaining = [times]
        real = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.addCleanup(real.close)
        with unittest.mock.patch.object(
            sqlite_store.sqlite3, "connect", return_value=locked_on_journal_mode(real, remaining)
        ):
            connection = open_database(self.path, wait_seconds=wait_seconds)
        self.addCleanup(connection.close)
        return connection, remaining

    def test_a_contended_journal_mode_is_waited_out(self) -> None:
        """A refused pragma is contention, and contention is not a reason to stop.

        Two brokers starting together, or any two touching the same file, is the
        documented deployment rather than an edge case. Before the wait existed,
        whichever connection lost the race raised at open, and the broker was a
        lock message in a log rather than a running service.
        """
        connection, remaining = self._open_with(times=2)
        self.assertEqual(remaining[0], 0, "the journal mode was never retried")
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal", "the database was left out of WAL mode")

    def test_a_journal_mode_lock_that_never_clears_is_raised(self) -> None:
        """The ceiling is what keeps a stuck lock a refusal rather than a hang.

        And the message names the budget, because an operator reading a startup log
        can otherwise only see that the broker refused to start. What they need is
        that the database was reachable and something else held the journal-mode
        switch for as long as it waited.
        """
        with self.assertRaises(sqlite3.OperationalError) as raised:
            self._open_with(times=99, wait_seconds=3.0)
        self.assertIn("journal mode", str(raised.exception))
        self.assertIn("3s", str(raised.exception))

    def test_an_unrelated_error_is_not_waited_out(self) -> None:
        # waiting on a corrupt file or a missing directory would only delay a
        # fault the operator has to see
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.addCleanup(connection.close)

        class Broken:
            row_factory = None

            def execute(self, statement, *args):
                if "journal_mode" in statement:
                    raise sqlite3.OperationalError("disk image is malformed")
                return connection.execute(statement, *args)

            def rollback(self):
                return connection.rollback()

            def close(self):
                return connection.close()

        with unittest.mock.patch.object(sqlite_store.sqlite3, "connect", return_value=Broken()):
            with self.assertRaises(sqlite3.OperationalError) as raised:
                open_database(self.path)
        self.assertIn("malformed", str(raised.exception))

    def test_each_store_keeps_the_transaction_semantics_it_had(self) -> None:
        """Autocommit would half-write a delivery or a usage record.

        Two of the five stores rely on sqlite3's implicit transaction management:
        they open a `with connection:` block, write more than one row inside it, and
        never call commit, so the block exiting is what makes the writes durable.
        Handed `isolation_level=None` every statement commits the moment it runs,
        the block still exits cleanly, and a failure between two writes leaves the
        first one behind. All 55 of their tests passed with that mutation, because
        none of them fails halfway through a write.

        Three of the five wanted autocommit and must keep it: they take the write
        lock themselves with BEGIN IMMEDIATE and would deadlock against a
        transaction sqlite3 opened for them.

        This asserts the declared level rather than provoking a half-write, which
        is the cheaper of the two and the one that fails under the mutation. The
        stronger version needs a store method that fails between two statements,
        and no such method exists in these two stores to provoke.
        """
        from cindral.artifact import ArtifactStore
        from cindral.cache import CacheStore
        from cindral.deliveries import DeliveryStore
        from cindral.jobs import JobStore
        from cindral.metering import MeteredLedger

        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            implicit = {
                "deliveries": (DeliveryStore(str(root / "deliveries.db")), ""),
                "metering": (MeteredLedger(str(root / "metering.db")), ""),
            }
            automatic = {
                "jobs": (JobStore(str(root / "jobs.db")), None),
                "cache": (CacheStore(root / "cache"), None),
                "artifacts": (ArtifactStore(root / "artifacts"), None),
            }

            for name, (store, expected) in {**implicit, **automatic}.items():
                with self.subTest(store=name):
                    self.assertEqual(_level(store), expected)
                    close = getattr(store, "close", None)
                    if callable(close):
                        close()

    def test_every_store_opens_its_database_the_same_way(self) -> None:
        """Five stores shared this setup by copy, and the copy is the defect.

        The race is in the pragma, so a store that kept its own copy kept the
        race. Asserting on the callers rather than on the helper is what makes
        this a test rather than a description.
        """
        for name in ("jobs", "metering", "deliveries", "cache", "artifact"):
            source = Path("src/cindral", f"{name}.py").read_text()
            with self.subTest(store=name):
                self.assertNotIn(
                    "PRAGMA journal_mode", source,
                    f"{name} opens its own database and keeps the journal-mode race",
                )
                self.assertIn("open_database", source, f"{name} does not use the shared opener")


if __name__ == "__main__":
    unittest.main()

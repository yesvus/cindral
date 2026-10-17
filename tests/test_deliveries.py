"""Tests for durable dispatch claims and restart recovery."""
import hashlib
import sqlite3
import tempfile
import threading
import unittest
import unittest.mock
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cindral import deliveries
from cindral.deliveries import DeliveryStore
from cindral.github import GitHubAPIError, GitHubDispatchError
from cindral.policy import Policy, load_dispatch_targets
from cindral.webhook_dispatch import (
    dispatch_record,
    recover_ready,
    replay_loop,
    should_fallback,
    state_to_status,
)


REPOSITORY = "example-org/example-app"


class DeliveryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "jobs.db")
        self.store = DeliveryStore(self.path)
        self.github = Mock()
        self.policy = Policy.load("config/policy.toml")

    def register(self, delivery: str = "delivery-1", sha: str = "a" * 40):
        return self.store.register(REPOSITORY, delivery, "payload-hash", "cindral-dispatch.yml", "main", {"cindral_lane": "fallback"}, sha)

    def test_accepted_delivery_survives_broker_restart(self) -> None:
        record = self.register()
        self.assertEqual(dispatch_record(self.store, self.github, record), ("accepted", ""))
        restarted = DeliveryStore(self.path)
        self.assertEqual(restarted.get(REPOSITORY, record.delivery).state, "accepted")
        self.assertEqual(dispatch_record(restarted, self.github, record), ("duplicate", ""))
        self.github.dispatch.assert_called_once()

    def test_ready_delivery_replays_after_restart(self) -> None:
        record = self.register()
        restarted = DeliveryStore(self.path)
        recover_ready(restarted, self.github, self.policy)
        self.assertEqual(restarted.get(REPOSITORY, record.delivery).state, "accepted")
        self.github.dispatch.assert_called_once_with(REPOSITORY, "cindral-dispatch.yml", "main", {"cindral_lane": "fallback"})

    def test_interrupted_attempt_is_uncertain_and_never_replayed(self) -> None:
        record = self.register()
        self.assertTrue(self.store.claim(REPOSITORY, record.delivery))
        restarted = DeliveryStore(self.path)
        self.assertEqual(restarted.recover(), 1)
        recover_ready(restarted, self.github, self.policy)
        self.assertEqual(restarted.get(REPOSITORY, record.delivery).state, "uncertain")
        self.github.dispatch.assert_not_called()

    def test_a_replay_that_cannot_resolve_a_delivery_says_so(self) -> None:
        self.github.dispatch.side_effect = GitHubAPIError("temporary")
        record = self.register()
        with patch("builtins.print") as printed:
            recover_ready(self.store, self.github, self.policy)
        reported = " ".join(str(call) for call in printed.call_args_list)
        self.assertIn(record.delivery, reported)
        self.assertIn("uncertain", reported)

    def test_known_rejection_and_unknown_transport_result_are_distinct(self) -> None:
        self.github.dispatch.side_effect = GitHubDispatchError("bad inputs", status_code=422)
        rejected = self.register()
        self.assertEqual(dispatch_record(self.store, self.github, rejected)[0], "rejected")
        # No status code means a URLError: the request never reached GitHub, so
        # no run exists. Reading it as a lost answer left the push with no check
        # on either lane, which is why the transport case is a rejection.
        self.github.dispatch.side_effect = GitHubDispatchError("timed out")
        unreachable = self.register("delivery-2")
        self.assertEqual(dispatch_record(self.store, self.github, unreachable)[0], "rejected")
        self.github.dispatch.side_effect = GitHubDispatchError("HTTP 500", 500)
        uncertain = self.register("delivery-3")
        self.assertEqual(dispatch_record(self.store, self.github, uncertain)[0], "uncertain")
        self.assertFalse(self.store.claim(REPOSITORY, rejected.delivery))
        self.assertFalse(self.store.claim(REPOSITORY, unreachable.delivery))
        self.assertFalse(self.store.claim(REPOSITORY, uncertain.delivery))

    def test_rate_limit_403_is_not_a_permanent_rejection(self) -> None:
        self.github.dispatch.side_effect = GitHubDispatchError("rate limit", status_code=403)
        record = self.register()
        self.assertEqual(dispatch_record(self.store, self.github, record)[0], "uncertain")
        self.assertEqual(self.store.get(REPOSITORY, record.delivery).state, "uncertain")

    def test_recovery_continues_after_result_write_failure(self) -> None:
        first = self.register()
        second = self.register("delivery-2")
        real_finish = self.store.finish
        failed = False

        def flaky_finish(*args):
            nonlocal failed
            if not failed:
                failed = True
                raise sqlite3.OperationalError("disk unavailable")
            return real_finish(*args)

        with patch.object(self.store, "finish", side_effect=flaky_finish):
            recover_ready(self.store, self.github, self.policy)
        self.assertEqual(self.store.get(REPOSITORY, first.delivery).state, "attempting")
        self.assertEqual(self.store.get(REPOSITORY, second.delivery).state, "accepted")
        self.assertEqual(self.github.dispatch.call_count, 2)
        self.assertEqual(self.store.recover(), 1)

    def test_removed_workflow_rejects_ready_delivery(self) -> None:
        record = self.register()
        self.github.workflow_exists.return_value = False
        recover_ready(self.store, self.github, self.policy)
        self.assertEqual(self.store.get(REPOSITORY, record.delivery).state, "rejected")
        self.github.dispatch.assert_not_called()

    def test_replay_loop_retries_ready_after_transient_lookup_failure(self) -> None:
        record = self.register()
        self.github.workflow_exists.side_effect = [GitHubAPIError("temporary"), True]
        stop = threading.Event()
        thread = threading.Thread(
            target=replay_loop, args=(self.store, self.github, self.policy, stop, 0.01)
        )
        thread.start()
        try:
            for _ in range(100):
                if self.store.get(REPOSITORY, record.delivery).state == "accepted":
                    break
                stop.wait(0.01)
            self.assertEqual(self.store.get(REPOSITORY, record.delivery).state, "accepted")
            self.github.dispatch.assert_called_once()
        finally:
            stop.set()
            thread.join(timeout=2)

    def test_result_cannot_overwrite_uncertain_attempt(self) -> None:
        record = self.register()
        self.assertTrue(self.store.claim(REPOSITORY, record.delivery))
        self.store.recover()
        with self.assertRaisesRegex(RuntimeError, "changed"):
            self.store.finish(REPOSITORY, record.delivery, "accepted")
        self.assertEqual(self.store.get(REPOSITORY, record.delivery).state, "uncertain")

    def test_concurrent_duplicate_claims_dispatch_once(self) -> None:
        record = self.register()
        started = threading.Event()
        release = threading.Event()

        def dispatch(*_args):
            started.set()
            self.assertTrue(release.wait(5))

        self.github.dispatch.side_effect = dispatch
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(dispatch_record, self.store, self.github, record)
            self.assertTrue(started.wait(5))
            second = pool.submit(dispatch_record, DeliveryStore(self.path), self.github, record)
            self.assertEqual(second.result(timeout=5)[0], "attempting")
            release.set()
            self.assertEqual(first.result(timeout=5)[0], "accepted")
        self.github.dispatch.assert_called_once()

    def test_eight_concurrent_claims_have_one_owner(self) -> None:
        record = self.register()
        start = threading.Barrier(8)

        def claim() -> bool:
            start.wait(timeout=5)
            return DeliveryStore(self.path).claim("EXAMPLE-ORG/EXAMPLE-APP", record.delivery)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: claim(), range(8)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 7)

    def _both_mode_policy(self) -> Policy:
        return replace(
            self.policy,
            dispatch_targets=load_dispatch_targets({"dispatch": {"repositories": [
                {"mode": "both", "repository": REPOSITORY,
                 "workflows": ["cindral-dispatch.yml"], "refs": ["refs/heads/main"]}]}}),
        )

    def test_a_replayed_refusal_reaches_the_device_fallback(self) -> None:
        # A READY row is a delivery whose first attempt never finished, and
        # nothing re-sends a delivery the broker already answered, so a refusal
        # found here is the one case where the push is otherwise lost outright.
        record = self.register()
        self.github.dispatch.side_effect = GitHubDispatchError("HTTP 422", 422)
        queued = Mock()
        recover_ready(self.store, self.github, self._both_mode_policy(), fallback=queued)
        # the whole record, so the fallback can enqueue the commit it names
        self.assertEqual([(r.repository, r.sha) for r in queued.call_args.args], [(REPOSITORY, "a" * 40)])
        self.assertEqual(self.store.get(REPOSITORY, record.delivery).state, "rejected")

    def test_a_replayed_lost_answer_is_never_queued_on_a_device(self) -> None:
        # UNCERTAIN is a 5xx, where a run may already exist, so the same commit
        # must not also run on a device and report itself twice
        record = self.register()
        self.github.dispatch.side_effect = GitHubDispatchError("HTTP 502", 502)
        queued = Mock()
        recover_ready(self.store, self.github, self._both_mode_policy(), fallback=queued)
        queued.assert_not_called()
        self.assertEqual(self.store.get(REPOSITORY, record.delivery).state, "uncertain")

    def test_a_dispatch_only_repository_is_not_queued_by_a_replay(self) -> None:
        # mode = "dispatch" names no device, so a refused push for one stays
        # refused rather than reaching local hardware the policy never offered
        self.register()
        self.github.dispatch.side_effect = GitHubDispatchError("HTTP 422", 422)
        dispatch_only = replace(
            self.policy,
            dispatch_targets=load_dispatch_targets({"dispatch": {"repositories": [
                {"mode": "dispatch", "repository": REPOSITORY,
                 "workflows": ["cindral-dispatch.yml"], "refs": ["refs/heads/main"]}]}}),
        )
        queued = Mock()
        with patch("builtins.print") as printed:
            recover_ready(self.store, self.github, dispatch_only, fallback=queued)
        queued.assert_not_called()
        self.assertIn("ran nowhere", " ".join(str(call) for call in printed.call_args_list))

    def test_a_ledger_written_before_the_commit_column_still_opens(self) -> None:
        # A broker restarting onto a database from an older build must not find
        # its own ledger unreadable. The row keeps the state it had, and carries
        # no commit, which is what the replay then says out loud.
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE webhook_deliveries")
            connection.execute(
                "CREATE TABLE webhook_deliveries ("
                "repository TEXT NOT NULL, delivery TEXT NOT NULL, fingerprint TEXT NOT NULL,"
                "workflow TEXT NOT NULL, ref TEXT NOT NULL, inputs TEXT NOT NULL,"
                "state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,"
                "error TEXT, PRIMARY KEY (repository, delivery))"
            )
            connection.execute(
                "INSERT INTO webhook_deliveries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (REPOSITORY, "delivery-old", "payload-hash", "cindral-dispatch.yml",
                 "main", '{"cindral_lane": "fallback"}', "ready", 0.0, 0.0, None),
            )
        migrated = DeliveryStore(self.path).get(REPOSITORY, "delivery-old")
        self.assertEqual((migrated.state, migrated.sha), ("ready", ""))

    def test_a_replay_without_a_recorded_commit_says_so(self) -> None:
        # a row written before the ledger carried the commit, and a device
        # checks a commit out rather than re-reading a payload it never has
        self.github.dispatch.side_effect = GitHubDispatchError("HTTP 422", 422)
        self.register(sha="")
        queued = Mock()
        with patch("builtins.print") as printed:
            recover_ready(self.store, self.github, self._both_mode_policy(), fallback=queued)
        queued.assert_not_called()
        self.assertIn("no commit was recorded", " ".join(str(call) for call in printed.call_args_list))

    def test_a_removed_adapter_reaches_the_device_fallback(self) -> None:
        # GitHub cannot serve a repository whose adapter is gone either, so the
        # refusal is the same one the first attempt would have found
        self.register()
        self.github.workflow_exists.return_value = False
        queued = Mock()
        recover_ready(self.store, self.github, self._both_mode_policy(), fallback=queued)
        queued.assert_called_once()
        self.assertEqual(queued.call_args.args[0].repository, REPOSITORY)
        self.github.dispatch.assert_not_called()

    def test_a_settled_delivery_is_not_offered_to_a_fallback_twice(self) -> None:
        # A delivery another attempt already refused is not this caller's
        # outcome. Reading it as one queues a second device job for a dispatch
        # that never happened under this attempt, so the state belongs to the
        # winner of the claim and the loser is told the delivery is handled.
        self.github.dispatch.side_effect = GitHubDispatchError("HTTP 422", 422)
        record = self.register()
        self.assertTrue(self.store.claim(REPOSITORY, record.delivery))
        self.store.finish(REPOSITORY, record.delivery, "rejected", "HTTP 422")
        self.assertEqual(dispatch_record(self.store, self.github, record), ("duplicate", ""))
        self.assertFalse(should_fallback("duplicate"))
        self.github.dispatch.assert_not_called()

    def test_replay_limits_each_pass_to_thirty_deliveries(self) -> None:
        for index in range(31):
            self.register(f"delivery-{index:02d}")
        recover_ready(self.store, self.github, self.policy)
        self.assertEqual(self.github.dispatch.call_count, 30)
        self.assertEqual(self.store.get(REPOSITORY, "delivery-00").state, "accepted")
        self.assertEqual(self.store.get(REPOSITORY, "delivery-30").state, "ready")

    def test_unknown_delivery_state_returns_retryable_status(self) -> None:
        self.assertEqual(state_to_status("future-state"), 503)
        self.assertEqual(state_to_status("rejected"), 422)

    def test_delivery_id_keeps_its_original_payload(self) -> None:
        original = self.register()
        duplicate = self.store.register(REPOSITORY, original.delivery, hashlib.sha256(b"other").hexdigest(),
                                        "cindral-dispatch.yml", "main", {"cindral_lane": "hosted"})
        self.assertEqual(duplicate, original)


class ConcurrentMigrationTest(unittest.TestCase):
    """Two processes opening one pre-migration ledger must both come up.

    The read that decides whether to migrate and the write that migrates are not
    one atomic step, so a rolling deploy can have two brokers race. SQLite
    serialises the writes and rejects the loser with `duplicate column name`,
    which used to be a hard startup failure on a ledger that was perfectly fine.
    """

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = str(Path(self._dir.name) / "deliveries.db")
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE webhook_deliveries ("
                "repository TEXT NOT NULL, delivery TEXT NOT NULL, fingerprint TEXT NOT NULL,"
                "workflow TEXT NOT NULL, ref TEXT NOT NULL, inputs TEXT NOT NULL,"
                "state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,"
                "error TEXT, PRIMARY KEY (repository, delivery))"
            )

    def test_two_stores_racing_the_migration_both_come_up(self) -> None:
        # Two, not eight: the documented deployment is one replica, so the only
        # real race is a rolling deploy where an old and a new process overlap.
        # Eight threads contend on SQLite's write lock long enough to exceed
        # busy_timeout, which is a different failure and would make this test
        # pass or fail on timing rather than on the migration.
        errors: list[BaseException] = []
        stores: list[DeliveryStore] = []
        barrier = threading.Barrier(2)

        def open_store() -> None:
            try:
                barrier.wait(timeout=10)
                stores.append(DeliveryStore(self.path))
            except BaseException as exc:  # noqa: BLE001 - the assertion reports it
                errors.append(exc)

        threads = [threading.Thread(target=open_store) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        # The joins are the assertion. A thread that has not finished contributes
        # no store, and reporting that as "one store came up" sends whoever reads
        # the failure looking at the migration rather than at a loaded machine.
        self.assertEqual(errors, [])
        self.assertFalse([t for t in threads if t.is_alive()], "a store never opened")
        self.assertEqual(len(stores), 2)
        for store in stores:
            self.assertEqual(store.pending(), [])

    def test_a_locked_ledger_is_waited_out_rather_than_refused(self) -> None:
        """A lock says nothing about the schema, so it is not a lost race.

        The other test in this class races two brokers for the same `ALTER` and
        the loser sees a duplicate column, which proves the winner finished. A
        locked database proves nothing of the kind: the column may still be
        missing, so re-reading once and giving up turns a moment of contention
        into a broker that will not start on a ledger that is perfectly fine.

        The lock is injected rather than raced for. Holding a real write lock
        long enough to outlast the connection's own busy timeout turns this into
        a test of how many seconds a loaded machine took, and that is the test
        that has been flaking in this class all night. What matters is the
        decision: a lock is retried, a duplicate is resolved by re-reading, and
        anything else is raised.
        """
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.addCleanup(connection.close)
        # the same row factory the store's own connections carry, so the
        # migration reads a row the way it does in the broker
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")

        locked = {"left": 2}

        class Locked:
            """A connection whose ALTER is locked twice, then works."""

            def execute(self, statement, *args):
                if statement.startswith("ALTER") and locked["left"]:
                    locked["left"] -= 1
                    raise sqlite3.OperationalError("database is locked")
                return connection.execute(statement, *args)

            def rollback(self):
                connection.rollback()

        DeliveryStore._migrate(Locked())  # type: ignore[arg-type]

        columns = {row["name"] for row in connection.execute("PRAGMA table_info(webhook_deliveries)")}
        self.assertIn("sha", columns, "the migration gave up on a locked ledger")
        self.assertEqual(locked["left"], 0, "the migration never retried")

    def test_a_lock_the_migration_cannot_wait_out_is_raised(self) -> None:
        # the ceiling exists so a lock that never clears is a refusal with a
        # cause, not a broker that hangs at startup for ever
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.addCleanup(connection.close)
        # the same row factory the store's own connections carry, so the
        # migration reads a row the way it does in the broker
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")

        class AlwaysLocked:
            def execute(self, statement, *args):
                if statement.startswith("ALTER"):
                    raise sqlite3.OperationalError("database is locked")
                return connection.execute(statement, *args)

            def rollback(self):
                connection.rollback()

        with unittest.mock.patch.object(deliveries, "MIGRATION_LOCK_WAIT_SECONDS", 0.0):
            with self.assertRaises(sqlite3.OperationalError):
                DeliveryStore._migrate(AlwaysLocked())  # type: ignore[arg-type]

    def test_the_column_is_added_exactly_once(self) -> None:
        DeliveryStore(self.path)
        DeliveryStore(self.path)
        with sqlite3.connect(self.path) as connection:
            names = [row[1] for row in connection.execute("PRAGMA table_info(webhook_deliveries)")]
        self.assertEqual(names.count("sha"), 1)


class DeviceOnlyReplayTest(unittest.TestCase):
    """A repository the policy serves on a device must still build when replayed.

    The `DispatchNotPermitted` branch used to reject and move on, so a
    device-only repository whose refs were narrowed after its row was registered
    had its push dropped with nothing running anywhere. The docs promise the
    device lane still runs it, so the code has to keep that promise.
    """

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DeliveryStore(str(Path(self._dir.name) / "deliveries.db"))
        self.github = Mock()
        self.policy = Policy.load("config/policy.toml")

    def device_only(self) -> Policy:
        return replace(
            self.policy,
            dispatch_targets=load_dispatch_targets({"dispatch": {"repositories": [
                {"mode": "device", "repository": REPOSITORY, "workflows": ["cindral-dispatch.yml"]}]}}),
        )

    def test_a_device_only_replay_queues_the_run(self) -> None:
        self.store.register(REPOSITORY, "d-1", "hash", "cindral-dispatch.yml", "main", {}, "a" * 40)
        queued = Mock()
        recover_ready(self.store, self.github, self.device_only(), fallback=queued)
        queued.assert_called_once()
        self.assertEqual(queued.call_args.args[0].sha, "a" * 40)

    def test_a_dispatch_only_replay_is_still_refused(self) -> None:
        # the control: the device is offered only where the policy names one, so
        # the fix cannot be read as "always queue on a replay"
        dispatch_only = replace(
            self.policy,
            dispatch_targets=load_dispatch_targets({"dispatch": {"repositories": [
                {"mode": "dispatch", "repository": REPOSITORY, "workflows": ["cindral-dispatch.yml"],
                 "refs": ["refs/heads/main"]}]}}),
        )
        self.store.register(REPOSITORY, "d-2", "hash", "cindral-dispatch.yml", "main", {}, "a" * 40)
        self.github.dispatch.side_effect = GitHubDispatchError("HTTP 422", 422)
        queued = Mock()
        with patch("builtins.print"):
            recover_ready(self.store, self.github, dispatch_only, fallback=queued)
        queued.assert_not_called()

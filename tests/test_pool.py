import tempfile
import unittest
from pathlib import Path

from cindral.jobs import JobStore
from cindral.models import Runner
from cindral.pool import pool_snapshot, render_metrics


class PoolSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = JobStore(str(Path(self._tmp.name) / "jobs.db"))

    def snapshot(self, runners=(), now=None, include_recent=True):
        connection = self.store._connect()
        self.addCleanup(connection.close)
        return pool_snapshot(connection, runners, now, include_recent)

    def test_expired_lease_is_reported_as_pending(self) -> None:
        # a running row past its lease is awaiting reclaim, so reporting it as
        # running would put a job on a device the panel shows as idle
        self.store.enqueue("org/app", "sha1", "main", command=("ci",), now=100.0)
        self.store.claim("server", [], lease_seconds=30, now=100.0)

        live = self.snapshot((Runner("server", "online", False, ("linux",)),), now=120.0)
        self.assertEqual(live["queue_depth"]["running"], 1)
        self.assertEqual(live["expired_lease_count"], 0)

        runners = (Runner("server", "online", False, ("linux",)),)
        expired = self.snapshot(runners, now=140.0)
        self.assertEqual(expired["queue_depth"]["running"], 0)
        self.assertEqual(expired["queue_depth"]["pending"], 1)
        self.assertEqual(expired["expired_lease_count"], 1)
        self.assertFalse(expired["devices"][0]["busy"])
        self.assertIsNone(expired["devices"][0]["current_lease"])

    def test_every_lease_on_a_device_is_listed(self) -> None:
        # a double claim stays visible instead of one lease overwriting the
        # other while the running count still counts both
        self.store.enqueue("org/a", "sha1", "main", command=("ci",), now=100.0)
        self.store.enqueue("org/b", "sha2", "main", command=("ci",), now=101.0)
        first = self.store.claim("server", [], lease_seconds=300, now=110.0)
        second = self.store.claim("server", [], lease_seconds=300, now=120.0)

        runners = (Runner("server", "online", False, ("linux",)),)
        snapshot = self.snapshot(runners, now=130.0)
        self.assertEqual(snapshot["queue_depth"]["running"], 2)
        leases = snapshot["devices"][0]["leases"]
        self.assertEqual({lease["job_id"] for lease in leases}, {first.id, second.id})
        # the soonest expiry is the one that frees the slot first
        self.assertEqual(snapshot["devices"][0]["current_lease"]["job_id"], first.id)

    def test_a_lease_device_absent_from_state_is_still_listed(self) -> None:
        self.store.enqueue("org/app", "sha1", "main", command=("ci",), now=100.0)
        self.store.claim("dynamic", [], lease_seconds=300, now=110.0)

        snapshot = self.snapshot((), now=120.0)
        self.assertEqual([d["name"] for d in snapshot["devices"]], ["dynamic"])
        self.assertTrue(snapshot["devices"][0]["busy"])

    def test_include_recent_skips_the_log_column(self) -> None:
        # a metrics scrape must not pay for the log of every recent job
        self.store.enqueue("org/app", "sha1", "main", command=("ci",))
        snapshot = self.snapshot(include_recent=False)
        self.assertEqual(snapshot["recent_jobs"], [])
        self.assertEqual(snapshot["queue_depth"]["pending"], 1)

    def test_a_long_log_is_truncated_to_a_tail_preview(self) -> None:
        self.store.enqueue("org/app", "sha1", "main", command=("ci",))
        job = self.store.claim("server", [], lease_seconds=300, now=100.0)
        assert job is not None
        self.store.report(
            job.id, "server", 0, now=110.0, log="x" * (64 * 1024 + 100)
        )

        recent = self.snapshot(now=120.0)["recent_jobs"][0]
        self.assertEqual(len(recent["log"]), 4000)
        self.assertTrue(recent["log_truncated"])

    def test_metrics_render_without_a_queue(self) -> None:
        text = render_metrics((Runner("server", "online", False, ()),), None)
        self.assertIn('cindral_devices{status="online"} 1', text)
        self.assertIn('cindral_devices{status="offline"} 0', text)
        self.assertNotIn("cindral_queue_depth", text)

    def test_metrics_render_device_and_queue_gauges(self) -> None:
        self.store.enqueue("org/app", "sha1", "main", command=("ci",))
        runners = (
            Runner("server", "online", False, ("linux",)),
            Runner("desktop", "offline", False, ("linux",)),
        )
        text = render_metrics(runners, self.snapshot(runners))
        self.assertIn('cindral_devices{status="online"} 1', text)
        self.assertIn('cindral_devices{status="offline"} 1', text)
        self.assertIn('cindral_queue_depth{state="pending"} 1', text)
        self.assertIn("cindral_devices_busy 0", text)

    def test_metrics_render_surfaces_expired_leases(self) -> None:
        self.store.enqueue("org/app", "sha1", "main", command=("ci",), now=100.0)
        self.store.claim("server", [], lease_seconds=30, now=100.0)
        text = render_metrics((), self.snapshot(now=140.0))
        self.assertIn("cindral_expired_leases 1", text)
        self.assertIn('cindral_queue_depth{state="pending"} 1', text)

    def test_store_metrics_renders_the_same_text(self) -> None:
        self.store.enqueue("org/app", "sha1", "main", command=("ci",))
        text = self.store.metrics((Runner("server", "online", False, ()),))
        self.assertIn('cindral_queue_depth{state="pending"} 1', text)


if __name__ == "__main__":
    unittest.main()

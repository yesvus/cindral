import tempfile
import unittest
from pathlib import Path

from cindral import jobs


class JobStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = jobs.JobStore(str(Path(self._tmp.name) / "jobs.db"))

    def test_enqueue_claim_report_lifecycle(self) -> None:
        job = self.store.enqueue("example-org/example-app", "abc123", "main", ("pnpm", "run", "ci"))
        self.assertEqual(job.status, jobs.PENDING)

        claimed = self.store.claim("server", ["self-hosted", "Linux", "ARM64"], now=100.0)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, job.id)
        self.assertEqual(claimed.status, jobs.RUNNING)
        self.assertEqual(claimed.device, "server")
        self.assertEqual(claimed.attempts, 1)

        reported = self.store.report(job.id, "server", 0, now=100.0)
        self.assertEqual(reported.status, jobs.SUCCESS)
        self.assertEqual(reported.exit_code, 0)
        self.assertIsNone(reported.lease_expires)

    def test_nonzero_exit_is_failure(self) -> None:
        job = self.store.enqueue("example-org/example-app", "abc123", "main", ("pnpm", "run", "ci"))
        self.store.claim("server", [], now=100.0)
        reported = self.store.report(job.id, "server", 17, now=100.0)
        self.assertEqual(reported.status, jobs.FAILURE)
        self.assertEqual(reported.exit_code, 17)

    def test_claim_requires_matching_labels(self) -> None:
        self.store.enqueue("example-org/example-app", "abc123", "main", ("pnpm", "run", "ci"), labels=("arm64",))
        self.assertIsNone(self.store.claim("server", ["x64"], now=100.0))
        claimed = self.store.claim("server", ["arm64", "linux"], now=100.0)
        self.assertIsNotNone(claimed)

    def test_claim_is_empty_when_no_pending_jobs(self) -> None:
        self.assertIsNone(self.store.claim("server", []))
        self.store.enqueue("example-org/example-app", "abc123", "main", ("pnpm", "run", "ci"))
        self.store.claim("server", [])
        self.assertIsNone(self.store.claim("burst", []))

    def test_claim_returns_oldest_first(self) -> None:
        first = self.store.enqueue("example-org/a", "sha1", "main", ("ci",), now=1.0)
        self.store.enqueue("example-org/b", "sha2", "main", ("ci",), now=2.0)
        claimed = self.store.claim("server", [], now=100.0)
        self.assertEqual(claimed.id, first.id)

    def test_expired_lease_is_reclaimed(self) -> None:
        job = self.store.enqueue("example-org/example-app", "abc123", "main", ("pnpm", "run", "ci"))
        self.store.claim("burst", [], lease_seconds=30, now=100.0)
        self.assertEqual(self.store.reclaim_expired(now=129.0), 0)
        self.assertEqual(self.store.reclaim_expired(now=131.0), 1)
        requeued = self.store.get(job.id)
        self.assertEqual(requeued.status, jobs.PENDING)
        self.assertIsNone(requeued.device)
        claimed = self.store.claim("server", [], now=200.0)
        self.assertEqual(claimed.id, job.id)
        self.assertEqual(claimed.attempts, 2)

    def test_claim_reclaims_an_expired_lease_first(self) -> None:
        job = self.store.enqueue("example-org/example-app", "abc123", "main", ("pnpm", "run", "ci"))
        self.store.claim("burst", [], lease_seconds=30, now=100.0)
        # no explicit reclaim call: the abandoned job must be reclaimed by claim
        reclaimed = self.store.claim("server", [], now=131.0)
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed.id, job.id)
        self.assertEqual(reclaimed.device, "server")
        self.assertEqual(reclaimed.attempts, 2)

    def test_renew_extends_only_the_lease_holder(self) -> None:
        job = self.store.enqueue("example-org/example-app", "abc123", "main", ("pnpm", "run", "ci"))
        self.store.claim("burst", [], lease_seconds=30, now=100.0)
        self.assertTrue(self.store.renew(job.id, "burst", lease_seconds=30, now=120.0))
        # renewed expiry is 150.0; the original 130.0 is between the two
        self.assertEqual(self.store.reclaim_expired(now=140.0), 0)
        self.assertEqual(self.store.reclaim_expired(now=151.0), 1)
        self.assertFalse(self.store.renew(job.id, "server", lease_seconds=30, now=120.0))

    def test_renew_rejects_an_expired_lease(self) -> None:
        job = self.store.enqueue("example-org/example-app", "abc123", "main", ("pnpm", "run", "ci"))
        self.store.claim("burst", [], lease_seconds=30, now=100.0)
        self.assertFalse(self.store.renew(job.id, "burst", lease_seconds=30, now=131.0))

    def test_report_rejects_an_expired_lease(self) -> None:
        job = self.store.enqueue("example-org/example-app", "abc123", "main", ("pnpm", "run", "ci"))
        self.store.claim("server", [], lease_seconds=30, now=100.0)
        with self.assertRaises(ValueError):
            self.store.report(job.id, "server", 0, now=131.0)

    def test_non_positive_lease_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.claim("server", [], lease_seconds=0)
        job = self.store.enqueue("example-org/example-app", "abc123", "main", ("pnpm", "run", "ci"))
        self.store.claim("server", [], now=100.0)
        with self.assertRaises(ValueError):
            self.store.renew(job.id, "server", lease_seconds=0)

    def test_report_rejects_a_different_device(self) -> None:
        job = self.store.enqueue("example-org/example-app", "abc123", "main", ("pnpm", "run", "ci"))
        self.store.claim("server", [], now=100.0)
        with self.assertRaises(ValueError):
            self.store.report(job.id, "burst", 0, now=100.0)

    def test_report_rejects_an_unleased_job(self) -> None:
        job = self.store.enqueue("example-org/example-app", "abc123", "main", ("pnpm", "run", "ci"))
        with self.assertRaises(ValueError):
            self.store.report(job.id, "server", 0)

    def test_report_rejects_unknown_job(self) -> None:
        with self.assertRaises(KeyError):
            self.store.report("missing", "server", 0)

    def test_enqueue_requires_a_sha(self) -> None:
        with self.assertRaises(ValueError):
            self.store.enqueue("example-org/a", "", "main", ("ci",))

    def test_command_is_optional(self) -> None:
        job = self.store.enqueue("example-org/a", "sha1", "main")
        self.assertEqual(job.command, ())
        self.assertEqual(self.store.get(job.id).command, ())

    def test_enqueue_is_idempotent_by_delivery(self) -> None:
        first = self.store.enqueue("example-org/a", "sha1", "main", ("ci",), delivery="delivery-1")
        second = self.store.enqueue("example-org/a", "sha1", "main", ("ci",), delivery="delivery-1")
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.store.list()), 1)

    def test_list_filters_by_status(self) -> None:
        first = self.store.enqueue("example-org/a", "sha1", "main", ("ci",))
        second = self.store.enqueue("example-org/b", "sha2", "main", ("ci",))
        self.store.claim("server", [])
        self.assertEqual([job.id for job in self.store.list(jobs.PENDING)], [second.id])
        self.assertEqual([job.id for job in self.store.list(jobs.RUNNING)], [first.id])
        self.assertEqual(len(self.store.list()), 2)

    def test_reclaim_count_tracks_expired_leases(self) -> None:
        self.assertEqual(self.store.reclaim_count(), 0)
        self.store.enqueue("example-org/app", "sha1", "main", ("ci",))
        self.store.claim("server", [], lease_seconds=30, now=100.0)
        self.store.reclaim_expired(now=140.0)
        self.assertEqual(self.store.reclaim_count(), 1)

        # Claim again and reclaim via next claim
        self.store.claim("server", [], lease_seconds=30, now=150.0)
        self.store.claim("burst", [], now=190.0)
        self.assertEqual(self.store.reclaim_count(), 2)

    def test_pool_snapshot_returns_metrics_and_device_leases(self) -> None:
        from cindral.models import Runner

        runners = (
            Runner("server", "online", False, ("linux", "arm64")),
            Runner("desktop", "offline", False, ("linux",)),
        )

        # Job 1: pending
        job1 = self.store.enqueue("example-org/app", "sha1", "main", ("ci",), now=100.0)
        # Job 2: running on server
        job2 = self.store.enqueue("example-org/app", "sha2", "main", ("ci",), now=105.0)
        self.store.claim("server", ["linux", "arm64"], lease_seconds=300, now=110.0)
        # Job 3: success
        job3 = self.store.enqueue("example-org/app", "sha3", "main", ("ci",), now=90.0)
        self.store.claim("worker-dynamic", [], lease_seconds=300, now=95.0)
        self.store.report(job3.id, "worker-dynamic", 0, now=98.0)

        snapshot = self.store.pool_snapshot(runners, now=120.0)

        self.assertEqual(snapshot["queue_depth"]["pending"], 1)
        self.assertEqual(snapshot["queue_depth"]["running"], 1)
        self.assertEqual(snapshot["queue_depth"]["success"], 1)
        self.assertEqual(snapshot["queue_depth"]["failure"], 0)
        self.assertEqual(snapshot["success_count"], 1)
        self.assertEqual(snapshot["failure_count"], 0)
        # Devices list includes configured runners and their active lease
        server_dev = next(d for d in snapshot["devices"] if d["name"] == "server")
        self.assertTrue(server_dev["busy"])
        self.assertIsNotNone(server_dev["current_lease"])
        self.assertEqual(server_dev["current_lease"]["job_id"], job1.id)
        self.assertEqual(server_dev["current_lease"]["remaining_seconds"], 290.0)

        desktop_dev = next(d for d in snapshot["devices"] if d["name"] == "desktop")
        self.assertFalse(desktop_dev["busy"])
        self.assertIsNone(desktop_dev["current_lease"])

        self.assertEqual(snapshot["oldest_pending_age_seconds"], 15.0)

        self.assertEqual(len(snapshot["recent_jobs"]), 3)


if __name__ == "__main__":
    unittest.main()

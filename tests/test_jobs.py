import json
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from cindral import jobs
from cindral.sole_holder import QueueHeld
from cindral.jobs import AGENT_RETENTION_SECONDS
from cindral.pool import pool_snapshot
from cindral.retry import (
    BASE_BACKOFF_SECONDS,
    LEASE_LOST,
    MAX_ATTEMPTS,
    MAX_BACKOFF_SECONDS,
    MAX_ERROR_CHARS,
    backoff_seconds,
    parked,
)


class JobStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = jobs.JobStore(str(Path(self._tmp.name) / "jobs.db"))

    def snapshot(self, store=None, now=None):
        store = store or self.store
        connection = store._connect()
        self.addCleanup(connection.close)
        return pool_snapshot(connection, now=now)

    def test_a_database_written_before_the_build_column_still_opens(self) -> None:
        # a broker upgraded in place has the old agents table. Refusing to open
        # would take every device offline at the moment they need the guard most
        path = Path(self._tmp.name) / "old.db"
        legacy = sqlite3.connect(path)
        legacy.executescript(
            "CREATE TABLE agents (device TEXT PRIMARY KEY, labels TEXT NOT NULL,"
            " max_concurrency INTEGER NOT NULL, last_seen REAL NOT NULL);"
        )
        legacy.execute("INSERT INTO agents VALUES ('server', '[]', 1, ?)", (time.time(),))
        legacy.commit()
        legacy.close()
        store = jobs.JobStore(str(path))
        devices = self.snapshot(store, now=time.time())["devices"]
        self.assertEqual([d["name"] for d in devices], ["server"])
        self.assertEqual(devices[0]["build"], {})
        self.assertFalse(devices[0]["build_known"])

    def test_recorded_builds_survive_a_reopen(self) -> None:
        path = Path(self._tmp.name) / "builds.db"
        jobs.JobStore(str(path)).record_heartbeat(
            "server", ["arm64"], 2, build={"commit": "c" * 40, "version": "0.12.0"}
        )
        devices = self.snapshot(jobs.JobStore(str(path)), now=time.time())["devices"]
        self.assertEqual(devices[0]["build"], {"commit": "c" * 40, "version": "0.12.0"})

    def test_a_heartbeat_with_no_build_does_not_inherit_the_previous_one(self) -> None:
        # an agent that stops naming itself is unknown, not still current
        self.store.record_heartbeat("server", [], 1, build={"commit": "d" * 40})
        self.store.record_heartbeat("server", [], 1)
        devices = self.snapshot(self.store, now=time.time())["devices"]
        self.assertEqual(devices[0]["build"], {})

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
        self.assertEqual(self.store.reclaim_expired(now=129.0), [])
        self.assertEqual(len(self.store.reclaim_expired(now=131.0)), 1)
        requeued = self.store.get(job.id)
        self.assertEqual(requeued.status, jobs.PENDING)
        self.assertIsNone(requeued.device)
        claimed = self.store.claim("server", [], now=200.0)
        self.assertEqual(claimed.id, job.id)
        self.assertEqual(claimed.attempts, 2)

    def test_claim_does_not_release_a_lapsed_lease_on_its_own(self) -> None:
        # The claim endpoint calls `reclaim_expired` immediately before claiming,
        # and gives it a publisher so the status a release owes is written with
        # the release. A sweep inside `claim` could only fire for a lease that
        # lapsed in the gap between the two, and having no publisher it would
        # release that run silently, leaving the commit at the status it was
        # last given. So `claim` hands out work and nothing else.
        job = self.store.enqueue("example-org/example-app", "abc123", "main", ("pnpm", "run", "ci"))
        self.store.claim("burst", [], lease_seconds=30, now=100.0)
        self.assertIsNone(self.store.claim("server", [], now=131.0))
        held = self.store.get(job.id)
        self.assertEqual(held.status, jobs.RUNNING)
        # the two assertions that discriminate: no release, and no owed status
        self.assertIsNone(held.last_error)
        self.assertEqual(jobs.owed_statuses(self.store._connect()), [])

    def test_renew_extends_only_the_lease_holder(self) -> None:
        job = self.store.enqueue("example-org/example-app", "abc123", "main", ("pnpm", "run", "ci"))
        self.store.claim("burst", [], lease_seconds=30, now=100.0)
        self.assertTrue(self.store.renew(job.id, "burst", lease_seconds=30, now=120.0))
        # renewed expiry is 150.0; the original 130.0 is between the two
        self.assertEqual(self.store.reclaim_expired(now=140.0), [])
        self.assertEqual(len(self.store.reclaim_expired(now=151.0)), 1)
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

    def test_repository_with_a_running_job_yields_to_an_idle_repository(self) -> None:
        first = self.store.enqueue("example-org/a", "sha1", "main", now=100.0)
        self.store.enqueue("example-org/a", "sha2", "main", now=101.0)
        other = self.store.enqueue("example-org/b", "sha3", "main", now=102.0)
        self.assertEqual(self.store.claim("device-a", [], now=110.0, max_repository_running=2).id, first.id)
        self.assertEqual(self.store.claim("device-b", [], now=111.0, max_repository_running=2).id, other.id)

    def test_repository_cap_holds_until_a_run_finishes(self) -> None:
        jobs_a = [self.store.enqueue("example-org/a", f"sha{i}", "main", now=100.0 + i) for i in range(3)]
        job_b = self.store.enqueue("example-org/b", "sha-b", "main", now=104.0)
        self.assertEqual(self.store.claim("device-a", [], now=110.0, max_repository_running=2).id, jobs_a[0].id)
        self.assertEqual(self.store.claim("device-b", [], now=111.0, max_repository_running=2).id, job_b.id)
        self.assertEqual(self.store.claim("device-c", [], now=112.0, max_repository_running=2).id, jobs_a[1].id)
        self.assertIsNone(self.store.claim("device-d", [], now=113.0, max_repository_running=2))
        self.store.report(jobs_a[0].id, "device-a", 0, now=114.0)
        self.assertEqual(self.store.claim("device-d", [], now=115.0, max_repository_running=2).id, jobs_a[2].id)

    def test_fairness_respects_device_labels(self) -> None:
        self.store.enqueue("example-org/a", "sha1", "main", labels=("gpu",), now=100.0)
        eligible = self.store.enqueue("example-org/b", "sha2", "main", now=101.0)
        self.assertEqual(self.store.claim("cpu", [], now=110.0, max_repository_running=2).id, eligible.id)

    def test_repository_limit_must_be_positive(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive"):
            self.store.claim("device", [], max_repository_running=0)

    def test_parallel_claims_share_the_repository_cap(self) -> None:
        for index in range(4):
            self.store.enqueue("example-org/a", f"sha{index}", "main", now=100.0 + index)
        barrier = threading.Barrier(4)
        claimed = []
        failures = []

        def claim(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                job = self.store.claim(f"device-{index}", [], now=110.0, max_repository_running=1)
                if job is not None:
                    claimed.append(job.id)
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=claim, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(len(claimed), 1)

    def test_reclaim_count_tracks_expired_leases(self) -> None:
        self.assertEqual(self.store.reclaim_count(), 0)
        self.store.enqueue("example-org/app", "sha1", "main", ("ci",))
        self.store.claim("server", [], lease_seconds=30, now=100.0)
        self.store.reclaim_expired(now=140.0)
        self.assertEqual(self.store.reclaim_count(), 1)

        # and again, reclaimed the way the claim endpoint does it
        self.store.claim("server", [], lease_seconds=30, now=200.0)
        self.store.reclaim_expired(now=800.0)
        self.assertEqual(self.store.reclaim_count(), 2)

    def test_a_release_records_the_owed_status_before_the_run_is_claimable(self) -> None:
        # the ordering has to be the write, not the speed of a network call: if
        # the verdict were published after the release, a retry that finished
        # first could be overwritten by a slow post about the earlier loss
        job = self.store.enqueue("example-org/app", "sha1", "main", ("ci",))
        self.store.claim("burst", [], lease_seconds=30, now=100.0)
        self.store.reclaim_expired(now=140.0, publish=lambda lost: json.dumps(["pending", lost.id]))
        owed = self.store.owed_statuses()
        self.assertEqual([(record.id, status) for record, status in owed], [(job.id, json.dumps(["pending", job.id]))])
        # the run is claimable again, and the verdict it owes is already durable
        self.assertEqual(self.store.get(job.id).status, jobs.PENDING)
        self.store.clear_status(job.id)
        self.assertEqual(self.store.owed_statuses(), [])

    def test_a_release_without_a_publisher_owes_nothing(self) -> None:
        job = self.store.enqueue("example-org/app", "sha1", "main", ("ci",))
        self.store.claim("burst", [], lease_seconds=30, now=100.0)
        self.store.reclaim_expired(now=140.0)
        self.assertEqual(self.store.owed_statuses(), [])

    def lose(self, clock: float) -> str:
        """Queue a run, let a device claim it, and let that lease lapse."""
        job = self.store.enqueue("example-org/app", "sha1", "main", ("ci",))
        self.store.claim("burst", [], lease_seconds=30, now=clock)
        self.store.reclaim_expired(now=clock + 31)
        return job.id

    def exhaust(self, job_id: str, clock: float) -> float:
        """Lose the same run once per attempt, on a device that never comes back."""
        for _ in range(MAX_ATTEMPTS):
            self.store.claim("burst", [], lease_seconds=30, now=clock)
            clock += 31
            self.store.reclaim_expired(now=clock)
            clock += MAX_BACKOFF_SECONDS
        return clock

    def test_a_lapsed_lease_requeues_the_run_instead_of_failing_it(self) -> None:
        # a device that dies leaves no verdict, so the run goes back to the queue
        # rather than becoming a red commit for a change that never ran
        lost = self.store.get(self.lose(100.0))
        self.assertEqual(lost.status, jobs.PENDING)
        self.assertIsNone(lost.exit_code)
        self.assertIsNone(lost.device)
        self.assertEqual(lost.last_error, LEASE_LOST)
        self.assertEqual(lost.attempts, 1)

    def test_a_requeued_run_waits_out_its_backoff_before_the_next_claim(self) -> None:
        # a device that keeps dying would otherwise put the same run straight
        # back in front of the next claim
        job_id = self.lose(100.0)
        self.assertIsNone(self.store.claim("server", [], now=131.0))
        again = self.store.claim("server", [], now=131.0 + BASE_BACKOFF_SECONDS)
        self.assertEqual(again.id, job_id)
        self.assertEqual(again.attempts, 2)

    def test_a_run_past_the_attempt_cap_is_parked_and_never_claimed_again(self) -> None:
        job = self.store.enqueue("example-org/app", "sha1", "main", ("ci",))
        clock = self.exhaust(job.id, 100.0)
        parked = self.store.get(job.id)
        self.assertEqual(parked.status, jobs.DEAD)
        self.assertEqual(parked.attempts, MAX_ATTEMPTS)
        self.assertEqual(parked.last_error, LEASE_LOST)
        # a parked run stays listed for whoever reads the queue, and is never
        # handed to a device again
        self.assertEqual([j.id for j in self.store.list(jobs.DEAD)], [job.id])
        self.assertIsNone(self.store.claim("server", [], now=clock))

    def test_a_requeued_run_survives_a_restart(self) -> None:
        # a broker that comes back must find the requeued run requeued, backoff
        # included, or a restart hands the same run to the first device back
        job_id = self.lose(100.0)
        restarted = jobs.JobStore(self.store.path, sole_holder=False)
        requeued = restarted.get(job_id)
        self.assertEqual(requeued.status, jobs.PENDING)
        self.assertEqual(requeued.last_error, LEASE_LOST)
        self.assertIsNone(restarted.claim("server", [], now=131.0))
        self.assertEqual(restarted.claim("server", [], now=161.0).id, job_id)

    def test_a_second_broker_refuses_to_start_on_one_queue(self) -> None:
        # The status ordering is guarded by a lock in the broker that holds the
        # queue. A second broker pointed at the same file would post against a
        # row it does not own and could report a run with a verdict that has
        # already been superseded, so it does not start.
        with self.assertRaisesRegex(QueueHeld, "already holds"):
            jobs.JobStore(self.store.path)
        # and a reader is not a broker, so it may still open the same file
        self.assertIsNotNone(jobs.JobStore(self.store.path, sole_holder=False).list())

    def test_a_broker_on_a_filesystem_without_flock_is_told_the_real_problem(self) -> None:
        # every flock failure is not a second broker, and saying so would be a
        # false diagnosis of a filesystem problem
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with mock.patch("cindral.sole_holder.fcntl.flock", side_effect=OSError(95, "not supported")):
            with self.assertRaises(OSError) as raised:
                jobs.JobStore(str(Path(directory.name) / "other.db"))
        self.assertNotIsInstance(raised.exception, QueueHeld)

    def test_a_parked_run_survives_a_restart(self) -> None:
        # a broker that comes back must find the parked run parked, so a
        # restart cannot hand the same dead work to the first device it sees
        job = self.store.enqueue("example-org/app", "sha1", "main", ("ci",))
        clock = self.exhaust(job.id, 100.0)

        restarted = jobs.JobStore(self.store.path, sole_holder=False)
        self.assertEqual(restarted.get(job.id).status, jobs.DEAD)
        self.assertEqual(restarted.get(job.id).last_error, LEASE_LOST)
        self.assertIsNone(restarted.claim("server", [], now=clock))

    def test_a_device_error_requeues_the_run_and_records_the_reason(self) -> None:
        job = self.store.enqueue("example-org/app", "sha1", "main", ("ci",))
        self.store.claim("server", [], lease_seconds=300, now=100.0)

        lost = self.store.report(job.id, "server", None, now=110.0, error="docker is not running")
        self.assertEqual(lost.status, jobs.PENDING)
        self.assertIsNone(lost.exit_code)
        self.assertIsNone(lost.device)
        self.assertEqual(lost.last_error, "docker is not running")
        self.assertIsNone(self.store.claim("burst", [], now=110.0))
        self.assertEqual(
            self.store.claim("burst", [], now=110.0 + BASE_BACKOFF_SECONDS).attempts,
            2,
        )

    def test_a_device_error_past_the_attempt_cap_parks_the_run(self) -> None:
        job = self.store.enqueue("example-org/app", "sha1", "main", ("ci",))
        clock = 100.0
        for _ in range(MAX_ATTEMPTS):
            self.assertIsNotNone(self.store.claim("server", [], lease_seconds=30, now=clock))
            clock += 1
            reported = self.store.report(job.id, "server", None, now=clock, error="no space left on device")
            clock += MAX_BACKOFF_SECONDS
        self.assertEqual(reported.status, jobs.DEAD)
        self.assertEqual(reported.attempts, MAX_ATTEMPTS)
        self.assertEqual(reported.last_error, "no space left on device")
        self.assertIsNone(self.store.claim("server", [], now=clock))

    def test_a_reported_failure_is_final_and_carries_no_device_error(self) -> None:
        # the loss that came before is history, and a run that reached a verdict
        # is not retried, so the two never read the same
        job = self.store.enqueue("example-org/app", "sha1", "main", ("ci",))
        self.store.claim("burst", [], lease_seconds=30, now=100.0)
        self.store.reclaim_expired(now=131.0)
        self.store.claim("server", [], lease_seconds=300, now=200.0)

        failed = self.store.report(job.id, "server", 17, now=201.0, log="cindral: step 2: pnpm test")
        self.assertEqual(failed.status, jobs.FAILURE)
        self.assertEqual(failed.exit_code, 17)
        self.assertEqual(failed.log, "cindral: step 2: pnpm test")
        self.assertIsNone(failed.last_error)
        self.assertIsNone(self.store.claim("burst", [], now=202.0))

    def test_a_report_with_neither_a_verdict_nor_an_error_is_refused(self) -> None:
        job = self.store.enqueue("example-org/app", "sha1", "main", ("ci",))
        self.store.claim("server", [], lease_seconds=300, now=100.0)
        with self.assertRaises(ValueError):
            self.store.report(job.id, "server", now=110.0)
        self.assertEqual(self.store.get(job.id).status, jobs.RUNNING)

    def test_the_record_carries_the_attempts_and_the_last_error(self) -> None:
        job = self.store.enqueue("example-org/app", "sha1", "main", ("ci",))
        self.store.claim("server", [], lease_seconds=300, now=100.0)
        self.store.report(job.id, "server", None, now=110.0, error="e" * (MAX_ERROR_CHARS + 50))

        payload = self.store.get(job.id).as_dict()
        self.assertEqual(payload["attempts"], 1)
        self.assertEqual(len(payload["last_error"]), MAX_ERROR_CHARS)
        self.assertIsNone(payload["exit_code"])

    def test_a_heartbeat_outlives_a_broker_restart(self) -> None:
        # presence in memory would make every device look agentless until its
        # next heartbeat, which is the gap the pool view exists to close
        self.store.record_heartbeat("device-alpha", ["arm64", "device"], 2, now=100.0)

        restarted = jobs.JobStore(self.store.path, sole_holder=False)
        device = self.snapshot(restarted, now=100.0)["devices"][0]
        self.assertEqual(device["name"], "device-alpha")
        self.assertEqual(device["labels"], ["arm64", "device"])
        self.assertTrue(device["agent_online"])
        self.assertEqual(device["max_concurrency"], 2)

    def test_the_latest_heartbeat_wins(self) -> None:
        self.store.record_heartbeat("device-alpha", ["arm64"], 1, now=100.0)
        self.store.record_heartbeat("device-alpha", ["arm64", "device"], 4, now=130.0)

        device = self.snapshot(self.store, now=140.0)["devices"][0]
        self.assertEqual(device["last_seen"], 130.0)
        self.assertEqual(device["max_concurrency"], 4)
        self.assertEqual(device["labels"], ["arm64", "device"])

    def test_a_heartbeat_keeps_presence_inside_the_retention_window(self) -> None:
        self.store.record_heartbeat("device-alpha", ["arm64"], 2, now=100.0)
        again = 100.0 + AGENT_RETENTION_SECONDS - 1
        self.store.record_heartbeat("device-alpha", ["arm64"], 2, now=again)

        devices = self.snapshot(self.store, now=again + 1)["devices"]
        self.assertEqual([d["name"] for d in devices], ["device-alpha"])

    def test_a_retired_agent_row_is_pruned(self) -> None:
        # presence that never comes back would sit in every snapshot as a device
        # with no agent, which is the signal the panel exists to raise
        self.store.record_heartbeat("retired", ["arm64"], 2, now=100.0)
        later = 100.0 + AGENT_RETENTION_SECONDS + 1
        self.store.record_heartbeat("device-alpha", ["arm64"], 2, now=later)

        devices = self.snapshot(self.store, now=later)["devices"]
        self.assertEqual([d["name"] for d in devices], ["device-alpha"])

    def test_a_retired_agent_row_is_pruned_on_read(self) -> None:
        # the read path prunes too, so a pool whose agents all go quiet still
        # shrinks without waiting for a heartbeat that will never arrive
        self.store.record_heartbeat("retired", ["arm64"], 2, now=100.0)
        later = 100.0 + AGENT_RETENTION_SECONDS + 1

        devices = self.snapshot(self.store, now=later)["devices"]
        self.assertEqual([d["name"] for d in devices], [])
        remaining = self.store._read(
            lambda connection: connection.execute("SELECT device FROM agents").fetchall()
        )
        self.assertEqual([row["device"] for row in remaining], [])


class RetryPolicyTest(unittest.TestCase):
    def test_the_wait_grows_with_each_attempt_and_then_holds_still(self) -> None:
        self.assertEqual(backoff_seconds(1), BASE_BACKOFF_SECONDS)
        self.assertEqual(backoff_seconds(2), 2 * BASE_BACKOFF_SECONDS)
        self.assertEqual(backoff_seconds(3), 4 * BASE_BACKOFF_SECONDS)
        self.assertEqual(backoff_seconds(9), MAX_BACKOFF_SECONDS)

    def test_the_attempt_cap_is_where_the_retries_end(self) -> None:
        self.assertFalse(parked(1))
        self.assertFalse(parked(MAX_ATTEMPTS - 1))
        self.assertTrue(parked(MAX_ATTEMPTS))
        self.assertTrue(parked(MAX_ATTEMPTS + 1))


if __name__ == "__main__":
    unittest.main()

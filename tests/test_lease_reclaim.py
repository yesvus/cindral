"""What releases a lease, on a claim or on a timer, and what it owes the commit."""
import contextlib
import io
import json
import tempfile
import sqlite3
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from cindral.github import GitHubAPIError
from cindral.jobs import DEAD, PENDING, RUNNING, SUCCESS, JobStore
from cindral.lease_reclaim import (
    LIVE_DEVICE_GRACE_SECONDS,
    RECLAIM_INTERVAL_SECONDS,
    post_if_current,
    reclaim_loop,
    reclaim_tick,
    status_publisher,
)
from cindral.retry import LEASE_LOST, MAX_ATTEMPTS


class Recorder:
    """Stands in for the publisher a tick posts through."""

    def __init__(self, failing: bool = False) -> None:
        self.posted: list[tuple[str, str, str]] = []
        self.failing = failing

    def __call__(self, job, state: str, description: str) -> None:
        if self.failing:
            raise GitHubAPIError("temporary")
        self.posted.append((job.id, state, description))


class FakeGitHub:
    def __init__(self, failing: bool = False) -> None:
        self.calls: list[tuple] = []
        self.failing = failing
        # the check run needs a base URL of its own, and without one the wrapper
        # raises instead of absorbing, which is how a call went unnoticed
        self.api_url = "https://api.github.test"

    def post_status(self, repository, sha, state, description="", context="", target_url="", timeout=30) -> None:
        if self.failing:
            raise GitHubAPIError("temporary")
        self.calls.append((repository, sha, state, description, context, target_url, timeout))


class ReclaimTickTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = JobStore(str(Path(self._tmp.name) / "jobs.db"))
        self.publisher = Recorder()

    def tick(self, now: float, publisher: Recorder | None = None) -> int:
        return reclaim_tick(self.store, publisher or self.publisher, now=now)

    def quietly(self, now: float, publisher: Recorder) -> int:
        """Tick with a publisher that cannot post, without the warning in the log."""
        with contextlib.redirect_stdout(io.StringIO()):
            return reclaim_tick(self.store, publisher, now=now)

    def claim(self, device: str = "device-alpha", now: float = 100.0, lease_seconds: int = 30):
        job = self.store.enqueue("org/app", "sha1", "main", ("ci",), now=now)
        claimed = self.store.claim(device, [], lease_seconds=lease_seconds, now=now)
        assert claimed is not None
        return job

    def test_a_lapsed_lease_is_released_without_a_claim(self) -> None:
        # a claim was the only thing that noticed a lapsed lease, so an idle pool
        # held the slot of a run no device was working on, and the commit showed
        # a check that would never finish
        job = self.claim()
        self.assertEqual(self.tick(140.0), 1)

        requeued = self.store.get(job.id)
        self.assertEqual(requeued.status, PENDING)
        self.assertIsNone(requeued.device)
        self.assertEqual(requeued.attempts, 1)
        self.assertEqual(self.store.reclaim_count(), 1)
        # the release recorded the status it owes and the same tick delivered
        # it, so nothing is left owed and the next claim has no status to repost
        self.assertEqual(self.store.owed_statuses(), [])
        self.assertEqual(len(self.publisher.posted), 1)

    def test_the_tick_parks_a_run_past_the_attempt_cap(self) -> None:
        # the same schedule that releases a retry has to reach the parked state,
        # or a dead device holds a run that is never handed out and never listed
        job = self.store.enqueue("org/app", "sha1", "main", ("ci",), now=100.0)
        clock = self.run_to_the_last_attempt(job.id, 100.0)

        self.assertEqual(self.tick(clock), 1)
        parked = self.store.get(job.id)
        self.assertEqual(parked.status, DEAD)
        self.assertEqual(parked.attempts, MAX_ATTEMPTS)
        self.assertIsNone(self.store.claim("device-alpha", [], now=clock + 1))

    def run_to_the_last_attempt(self, job_id: str, clock: float) -> float:
        """Lose the same run once per attempt, leaving the last lease held."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.assertIsNotNone(self.store.claim("device-alpha", [], lease_seconds=30, now=clock))
            clock += 31
            if attempt < MAX_ATTEMPTS:
                self.store.reclaim_expired(now=clock)
                clock += 400
        self.assertEqual(self.store.get(job_id).status, RUNNING)
        return clock

    def test_a_claim_and_a_tick_reclaim_the_same_run_once(self) -> None:
        # both paths release, and a run that both of them see must be counted
        # once or the reclaim counter stops meaning anything
        self.claim()
        barrier = threading.Barrier(2)
        released: list[int] = []

        def release_from_claim() -> None:
            barrier.wait(timeout=5)
            verdict = json.dumps(["pending", LEASE_LOST])
            released.append(len(self.store.reclaim_expired(now=140.0, publish=lambda job: verdict)))

        def release_from_tick() -> None:
            barrier.wait(timeout=5)
            released.append(self.tick(140.0))

        threads = [threading.Thread(target=body) for body in (release_from_claim, release_from_tick)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(sorted(released), [0, 1])
        self.assertEqual(self.store.reclaim_count(), 1)

    def test_a_parked_run_publishes_its_terminal_status_with_no_claim_in_between(self) -> None:
        # the drain ran on the claim path only, so a pool that went quiet left a
        # parked run's verdict owed and the commit on whatever it had last been
        # given
        job = self.store.enqueue("org/app", "sha1", "main", ("ci",), now=100.0)
        clock = self.run_to_the_last_attempt(job.id, 100.0)
        self.assertEqual(self.publisher.posted, [])

        self.tick(clock)
        self.assertEqual(self.store.get(job.id).status, DEAD)
        [(record, state, description)] = self.publisher.posted
        self.assertEqual(record, job.id)
        self.assertEqual(state, "error")
        self.assertIn(f"lost after {MAX_ATTEMPTS} attempts", description)
        self.assertIn(LEASE_LOST, description)
        # delivered once, and not owed again
        self.assertEqual(self.store.owed_statuses(), [])
        self.tick(clock + 31)
        self.assertEqual(len(self.publisher.posted), 1)

    def test_a_verdict_that_cannot_post_stays_owed_for_the_next_tick(self) -> None:
        job = self.claim()
        self.quietly(140.0, Recorder(failing=True))
        self.assertEqual(self.store.get(job.id).status, PENDING)
        self.assertEqual([record.id for record, _ in self.store.owed_statuses()], [job.id])

        self.tick(141.0)
        self.assertEqual(len(self.publisher.posted), 1)
        self.assertEqual(self.store.owed_statuses(), [])

    def test_the_timer_closes_the_check_the_run_was_opened_with(self) -> None:
        # a check opened when the run was queued has to be closed by whichever
        # path settles the run, or the commit shows a check in progress forever
        # while its status says the run is lost
        self.store.enqueue("org/app", "sha1", "main", status_sha="sha-status", now=100.0)
        self.store.claim("device-alpha", [], lease_seconds=30, now=100.0)
        github = FakeGitHub()
        closed: list[str] = []

        def close(job) -> None:
            closed.append(job.id)

        reclaim_tick(
            self.store,
            status_publisher(github, "cindral/ci", lambda job_id: f"https://panel/{job_id}"),
            now=140.0,
            check=close,
        )
        self.assertEqual(len(github.calls), 1, "the status was not posted")
        self.assertEqual(len(closed), 1, "the check run the run was opened with was not closed")

    def test_a_broker_with_no_github_client_reclaims_and_owes_nothing_forever(self) -> None:
        # a broker that cannot reach GitHub still has to release the lease, and
        # a verdict nothing can carry must not sit owed growing the queue
        job = self.claim()
        publish = status_publisher(None, "cindral/ci", lambda job_id: "")

        self.assertEqual(reclaim_tick(self.store, publish, now=140.0), 1)
        self.assertEqual(self.store.get(job.id).status, PENDING)
        self.assertEqual(self.store.owed_statuses(), [])

    def test_the_publisher_posts_the_run_own_commit(self) -> None:
        github = FakeGitHub()
        publish = status_publisher(github, "cindral/ci", lambda job_id: f"https://panel/{job_id}")
        job = self.store.enqueue("org/app", "sha1", "main", status_sha="sha-status", now=100.0)
        self.store.claim("device-alpha", [], lease_seconds=30, now=100.0)

        reclaim_tick(self.store, publish, now=140.0)
        self.assertEqual(len(github.calls), 1)
        self.assertEqual(github.calls[0][:3], ("org/app", "sha-status", "pending"))
        self.assertEqual(github.calls[0][4], "cindral/ci")
        self.assertEqual(github.calls[0][5], f"https://panel/{job.id}")
        # the post runs inside the lock the claim endpoint also takes, so its
        # timeout is the ceiling on how long a device waits for work
        self.assertLess(github.calls[0][6], 15, "the post can outlast a device's patience")
        self.assertIn(LEASE_LOST, github.calls[0][3])
        # the verdict is retired, not still owed: the post succeeded and nothing
        # is left for the next pass to send again
        self.assertEqual(self.store.owed_statuses(), [])

    def test_a_delivered_verdict_is_not_reposted_on_every_tick(self) -> None:
        # the claim endpoint already drains, so the tick's job is the gap. Once a
        # verdict is delivered it is dropped, or the tick republishes it every
        # interval and the backstop becomes the traffic.
        job = self.claim()
        self.tick(140.0)
        for _ in range(3):
            self.tick(200.0)
        self.assertEqual(len(self.publisher.posted), 1)
        self.assertEqual(self.publisher.posted[0][0], job.id)

    def test_a_verdict_superseded_before_the_drain_is_never_posted(self) -> None:
        # the drain read the owed verdict, the run was claimed again and finished
        # green, and the drain posted the loss anyway: the commit reported pending
        # for a run that had succeeded, and nothing would move it afterwards
        job = self.claim()
        self.quietly(140.0, Recorder(failing=True))
        self.store.claim("device-alpha", [], lease_seconds=300, now=200.0)
        self.store.clear_status(job.id)
        self.store.report(job.id, "device-alpha", 0, now=210.0)

        self.tick(220.0)
        self.assertEqual(self.store.get(job.id).status, SUCCESS)
        self.assertEqual(self.publisher.posted, [])
        self.assertEqual(self.store.owed_statuses(), [])

    def test_a_second_drain_while_the_first_is_posting_finds_nothing(self) -> None:
        # the timer and the claim path are two drains by construction, and a
        # verdict that reached the queue has to reach its commit once
        self.claim()
        self.quietly(140.0, Recorder(failing=True))
        posting, release = threading.Event(), threading.Event()
        posts: list[str] = []

        def slow_publish(record, state: str, description: str) -> None:
            posts.append(record.id)
            posting.set()
            release.wait(timeout=10)

        first = threading.Thread(target=lambda: self.tick(200.0, slow_publish), daemon=True)
        first.start()
        self.assertTrue(posting.wait(timeout=10), "the first drain never reached the post")
        # it is inside the post with the verdict already out of the row, which is
        # what the second drain reads
        self.assertEqual(self.store.owed_statuses(), [])
        self.assertEqual(self.tick(200.0), 0)
        release.set()
        first.join(timeout=10)

        self.assertEqual(len(posts), 1)
        self.assertEqual(self.store.owed_statuses(), [])

    def test_a_released_run_is_not_released_again(self) -> None:
        # a run the queue has already given back is not counted a second time and
        # does not spend another attempt
        job = self.claim()
        self.store.reclaim_expired(now=140.0, publish=lambda record: json.dumps(["pending", LEASE_LOST]))
        self.assertEqual(self.store.reclaim_count(), 1)

        self.assertEqual(self.tick(200.0), 0)
        self.assertEqual(self.store.reclaim_count(), 1)
        requeued = self.store.get(job.id)
        self.assertEqual(requeued.status, PENDING)
        self.assertEqual(requeued.attempts, 1)

    def test_the_reclaim_only_touches_a_run_the_queue_still_holds(self) -> None:
        # the reaper's compare-and-swap is the row's status, not its timestamps.
        # Only a path that hands a run back clears its lease, so a timestamp left
        # behind on a released row must not be enough to take the run again.
        self.store.enqueue("org/app", "sha1", "main", ("ci",), now=100.0)
        self.store._write(
            lambda connection: connection.execute(
                "UPDATE jobs SET status = ?, lease_expires = ?", (PENDING, 120.0)
            )
        )

        self.assertEqual(self.tick(400.0), 0)
        self.assertEqual(self.store.reclaim_count(), 0)
        self.assertEqual(self.store.get(self.store.list()[0].id).attempts, 0)


class ReclaimLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = JobStore(str(Path(self._tmp.name) / "jobs.db"))
        self.publisher = Recorder()

    def test_the_loop_reclaims_before_it_waits_and_stops_on_the_event(self) -> None:
        # the body runs before the first wait, so a broker that comes back
        # settles the leases its previous process left behind at once instead of
        # a minute later, and the event is what ends it
        job = self.store.enqueue("org/app", "sha1", "main", ("ci",))
        self.store.claim("device-alpha", [], lease_seconds=1, now=time.time() - 60)
        stop = threading.Event()
        delivered = threading.Event()

        def publish(record, state: str, description: str) -> None:
            delivered.set()
            stop.set()

        worker = threading.Thread(
            target=reclaim_loop, args=(self.store, publish, stop, RECLAIM_INTERVAL_SECONDS), daemon=True
        )
        worker.start()
        self.assertTrue(delivered.wait(timeout=10), "the loop never published the owed verdict")
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.store.reclaim_count(), 1)
        self.assertIsNotNone(self.store.get(job.id))

    def test_one_unreadable_verdict_does_not_lose_the_ones_after_it(self) -> None:
        # The batch is claimed before any of it is posted, so an exception on the
        # first used to abandon the rest, and the next pass claimed them again and
        # abandoned them at the same place
        first = self.store.enqueue("org/app", "sha1", "main")
        second = self.store.enqueue("org/app", "sha2", "main")
        for job in (first, second):
            self.store.claim("device-alpha", [], lease_seconds=1, now=100.0)
        self.store.reclaim_expired(now=1000.0, publish=lambda j: json.dumps(["error", "lost"]))
        # owed_statuses reads without claiming, and claim_owed_statuses would
        # take these two out of the row before the tick ever sees them
        self.assertEqual(len(self.store.owed_statuses()), 2)

        posted: list[str] = []
        real_seq = self.store.status_seq
        unreadable = first.id

        def flaky(job_id: str) -> int:
            if job_id == unreadable:
                raise sqlite3.OperationalError("database is locked")
            return real_seq(job_id)

        with unittest.mock.patch.object(
            self.store, "status_seq", side_effect=flaky
        ), contextlib.redirect_stdout(io.StringIO()):
            reclaim_tick(
                self.store,
                lambda job, state, description: posted.append(description),
                now=1000.0,
            )
        self.assertEqual(posted, ["lost"], "the readable verdict was not posted")
        # and the unreadable one is owed rather than lost
        self.assertEqual(len(self.store.owed_statuses()), 1)

    def test_a_tick_that_raises_is_reported_and_the_loop_continues(self) -> None:
        # a transient failure is the one thing that must not end the guarantee
        stop = threading.Event()
        attempts: list[int] = []

        class Broken:
            def reclaim_expired(self, now=None, publish=None):
                attempts.append(1)
                if len(attempts) == 1:
                    raise RuntimeError("database is locked")
                stop.set()
                return []

            def claim_owed_statuses(self):
                return []

            def status_seq(self, job_id: str) -> int:
                return 0

            def restore_status(self, job_id: str, status: str, seq: int) -> None:
                return None

        warnings = io.StringIO()
        with contextlib.redirect_stdout(warnings):
            worker = threading.Thread(
                target=reclaim_loop, args=(Broken(), self.publisher, stop, 0.01), daemon=True
            )
            worker.start()
            worker.join(timeout=10)

        self.assertFalse(worker.is_alive())
        self.assertGreaterEqual(len(attempts), 2)
        self.assertIn("lease reclaim paused", warnings.getvalue())


class LiveDeviceGraceTest(unittest.TestCase):
    """A lease is not spent while the device that holds it is still announcing."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = JobStore(str(Path(self._tmp.name) / "jobs.db"))
        self.publisher = Recorder()

    def beat(self, until: float, start: float = 100.0, device: str = "device-alpha") -> None:
        """Announce the device every ten seconds up to `until`, as an agent does."""
        for moment in range(int(start), int(until), 10):
            self.store.record_heartbeat(device, ["arm64"], 2, now=float(moment))

    def test_a_step_that_outlives_the_lease_keeps_its_run(self) -> None:
        # the lease is renewed on its own thread, so a long step is not what
        # loses it. What loses it is a renewal that arrives late, and releasing
        # then hands the same run to a second device while the first is on it.
        self.beat(135.0)
        job = self.store.enqueue("org/app", "sha1", "main", ("ci",), now=100.0)
        self.store.claim("device-alpha", [], lease_seconds=30, now=100.0)
        self.beat(135.0, start=105.0)

        self.assertEqual(reclaim_tick(self.store, self.publisher, now=135.0), 0)
        held = self.store.get(job.id)
        self.assertEqual(held.status, RUNNING)
        self.assertEqual(held.attempts, 1)
        self.assertEqual(self.store.reclaim_count(), 0)
        self.assertEqual(self.publisher.posted, [])

    def test_a_late_renewal_keeps_the_run_and_moves_the_lease(self) -> None:
        # the renewal is the device saying it is still working. Refusing it is
        # what cancels the executor on the device and restarts the run here.
        self.beat(135.0)
        job = self.store.enqueue("org/app", "sha1", "main", ("ci",), now=100.0)
        self.store.claim("device-alpha", [], lease_seconds=30, now=100.0)
        self.beat(135.0, start=105.0)

        self.assertTrue(self.store.renew(job.id, "device-alpha", lease_seconds=30, now=135.0))
        self.assertEqual(reclaim_tick(self.store, self.publisher, now=136.0), 0)
        self.assertEqual(self.store.get(job.id).status, RUNNING)
        self.assertEqual(self.store.get(job.id).lease_expires, 165.0)

    def test_a_run_that_finished_late_still_reports_its_verdict(self) -> None:
        # the work is done and the answer exists. Refusing it leaves a commit
        # with a running check and sends the finished build round again.
        self.beat(135.0)
        job = self.store.enqueue("org/app", "sha1", "main", ("ci",), now=100.0)
        self.store.claim("device-alpha", [], lease_seconds=30, now=100.0)
        self.beat(135.0, start=105.0)

        self.assertTrue(self.store.holds_lease(job.id, "device-alpha", now=135.0))
        self.assertEqual(self.store.report(job.id, "device-alpha", 0, now=135.0).status, SUCCESS)
        self.assertEqual(reclaim_tick(self.store, self.publisher, now=136.0), 0)

    def test_the_grace_is_bounded_so_a_stuck_device_cannot_hold_a_slot(self) -> None:
        # a device whose executor never returns keeps beating, so presence alone
        # would hold the run forever
        self.beat(195.0)
        job = self.store.enqueue("org/app", "sha1", "main", ("ci",), now=100.0)
        self.store.claim("device-alpha", [], lease_seconds=30, now=100.0)
        self.beat(195.0, start=105.0)

        self.assertEqual(reclaim_tick(self.store, self.publisher, now=131.0 + LIVE_DEVICE_GRACE_SECONDS), 1)
        self.assertEqual(self.store.get(job.id).status, PENDING)

    def test_a_device_that_stopped_announcing_loses_its_run_on_schedule(self) -> None:
        # the other half of the same rule: a device that is gone is the case the
        # reclaim exists for, and the grace must not delay it past the window
        self.store.record_heartbeat("device-alpha", ["arm64"], 2, now=100.0)
        job = self.store.enqueue("org/app", "sha1", "main", ("ci",), now=100.0)
        self.store.claim("device-alpha", [], lease_seconds=30, now=100.0)

        self.assertEqual(reclaim_tick(self.store, self.publisher, now=159.0), 0)
        self.assertEqual(reclaim_tick(self.store, self.publisher, now=161.0), 1)
        self.assertEqual(self.store.get(job.id).status, PENDING)

    def test_a_reclaim_from_a_device_still_there_is_counted_apart(self) -> None:
        # the split is the only record of whether a run was lost or restarted,
        # and a restart is the one that duplicates work
        self.beat(200.0)
        self.store.enqueue("org/app", "sha1", "main", ("ci",), now=100.0)
        self.store.claim("device-alpha", [], lease_seconds=30, now=100.0)
        self.beat(200.0, start=105.0)
        reclaim_tick(self.store, self.publisher, now=195.0)

        snapshot = self.store._read(
            lambda connection: dict(
                connection.execute("SELECT key, value FROM meta").fetchall()
            )
        )
        self.assertEqual(snapshot["reclaims"], 1)
        self.assertEqual(snapshot["reclaims_live"], 1)

    def test_a_run_with_no_device_behind_it_is_reclaimed_without_being_counted_live(self) -> None:
        self.store.enqueue("org/app", "sha1", "main", ("ci",), now=100.0)
        self.store.claim("device-alpha", [], lease_seconds=30, now=100.0)
        reclaim_tick(self.store, self.publisher, now=140.0)

        snapshot = self.store._read(
            lambda connection: dict(
                connection.execute("SELECT key, value FROM meta").fetchall()
            )
        )
        self.assertEqual(snapshot["reclaims"], 1)
        self.assertEqual(snapshot["reclaims_live"], 0)


class OwedVerdictTest(unittest.TestCase):
    """A release records the verdict it owes, and a later one replaces it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = JobStore(str(Path(self._tmp.name) / "jobs.db"))
        self.publisher = Recorder()

    def owed_description(self) -> str:
        [(record, status)] = self.store.owed_statuses()
        return json.loads(status)[1]

    def quietly(self, now: float, publisher: Recorder) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return reclaim_tick(self.store, publisher, now=now)

    def test_the_last_release_owes_its_own_verdict(self) -> None:
        # a run that lost its lease twice while the commit could not be reached
        # kept the first of the two verdicts, so a parked run reported the
        # second-to-last release and its commit stayed on a retry that will
        # never happen
        self.store.enqueue("org/app", "sha1", "main", ("ci",), now=100.0)
        self.store.claim("device-alpha", [], lease_seconds=30, now=100.0)
        self.quietly(140.0, Recorder(failing=True))
        first = self.owed_description()
        self.assertIn("retry 2 of 3", first)

        self.store.claim("device-alpha", [], lease_seconds=30, now=200.0)
        self.quietly(240.0, Recorder(failing=True))
        self.assertIn("retry 3 of 3", self.owed_description())

        self.store.claim("device-alpha", [], lease_seconds=30, now=300.0)
        self.quietly(340.0, Recorder(failing=True))
        parked = self.owed_description()
        self.assertIn(f"lost after {MAX_ATTEMPTS} attempts", parked)
        self.assertNotIn("retry", parked)

    def test_a_device_error_supersedes_the_loss_the_queue_owes(self) -> None:
        # The device says what happened to it, so that is the run's state. Leaving
        # the loss owed meant the sequence did not move, the drain's guard could
        # not see that it was stale, and a device error was reported to the commit
        # as a lost run.
        job = self.store.enqueue("org/app", "sha1", "main", ("ci",), now=100.0)
        self.store.claim("device-alpha", [], lease_seconds=30, now=100.0)
        self.quietly(140.0, Recorder(failing=True))
        self.assertEqual(len(self.store.owed_statuses()), 1, "nothing was owed to supersede")

        self.store.claim("device-alpha", [], lease_seconds=300, now=200.0)
        self.store.report(job.id, "device-alpha", None, now=210.0, error="docker is not running")
        self.assertEqual(self.store.owed_statuses(), [], "a superseded loss was still owed")
        self.assertEqual(self.store.get(job.id).status, PENDING)
        self.assertIn("docker is not running", self.store.get(job.id).last_error)

    def test_a_failed_post_does_not_restore_over_a_newer_verdict(self) -> None:
        # a release that lands while a post is in flight supersedes the verdict
        # this drain took, so the restore is refused and the run keeps the status
        # the queue owes for the state it is actually in
        job = self.store.enqueue("org/app", "sha1", "main", ("ci",), now=100.0)
        self.store.claim("device-alpha", [], lease_seconds=30, now=100.0)
        self.quietly(140.0, Recorder(failing=True))
        newer = json.dumps(["pending", "the second loss"])

        def fail_after_a_newer_release(record, state: str, description: str) -> None:
            self.store.claim("device-beta", [], lease_seconds=30, now=210.0)
            self.store.reclaim_expired(now=250.0, publish=lambda job: newer)
            raise GitHubAPIError("temporary")

        with contextlib.redirect_stdout(io.StringIO()):
            reclaim_tick(self.store, fail_after_a_newer_release, now=200.0)

        [(record, status)] = self.store.owed_statuses()
        self.assertEqual(record.id, job.id)
        self.assertEqual(status, newer)


if __name__ == "__main__":
    unittest.main()

"""The ordering of two verdicts for the same run, when the queue owes both.

The reclaim timer and the request path both drain what the queue owes, and a
verdict taken before a newer one was written has to be dropped. Otherwise a run
that has been reclaimed again is reported lost on the strength of the first loss.
"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cindral.github import GitHubAPIError
from cindral.jobs import SUCCESS, JobStore
from cindral.lease_reclaim import post_if_current


class OwedStatusOrderTest(unittest.TestCase):
    """Two drains, one run, and the older verdict must not land last.

    The reclaim timer and the request path both drain what the queue owes, and a
    verdict taken before a later one was written has to be dropped. Otherwise a
    run that has already been reported lost goes on being reported lost after the
    device came back and finished it.
    """

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = JobStore(str(Path(self.dir.name) / "jobs.db"))
        self.posted: list[str] = []

    def _lapse(self, job_id: str, claimed_at: float, message: str) -> None:
        """Claim a run and let its lease lapse, which owes one verdict."""
        self.store.claim("device-alpha", [], lease_seconds=1, now=claimed_at)
        self.store.reclaim_expired(
            now=claimed_at + 2, publish=lambda job: json.dumps(["error", message])
        )
    def _drain_current_only(self) -> None:
        """What a drain should do: post only the verdicts that are still latest."""
        for job, status, seq in self.store.claim_owed_statuses():
            post_if_current(self.store, job, status, seq, self._post)

    def _post(self, job, state, description) -> None:
        self.posted.append(f"drain posted {description}")

    def test_a_verdict_in_flight_is_dropped_when_a_newer_one_appears(self) -> None:
        # The interleaving that matters: the drain has taken a verdict and is
        # about to post it, a newer verdict is written and posted by the request
        # path, and the drain's post then lands last. That reports a run which has
        # already been reclaimed again as lost on the strength of the first loss.
        job = self.store.enqueue("org/app", "sha1", "main")
        self._lapse(job.id, 100.0, "lease lost")
        _job, stale_status, stale_seq = self.store.claim_owed_statuses()[0]

        # the newer verdict is written and published while the first is in hand
        self._lapse(job.id, 100_000.0, "lease lost again")
        for _owed, newer, _seq in self.store.claim_owed_statuses():
            self.posted.append(newer)
        self.assertIn('["error", "lease lost again"]', self.posted)

        sent = post_if_current(self.store, job, stale_status, stale_seq, self._post)
        self.assertFalse(sent, "a superseded verdict was posted")
        self.assertEqual(
            [line for line in self.posted if line.startswith("drain posted")], []
        )
        # superseded, not lost: it does not go back into the queue either
        self.assertEqual(len(self.store.claim_owed_statuses()), 0)

    def test_a_verdict_that_is_still_current_is_posted(self) -> None:
        job = self.store.enqueue("org/app", "sha1", "main")
        self._lapse(job.id, 100.0, "lease lost")
        _owed, status, seq = self.store.claim_owed_statuses()[0]
        self.assertTrue(post_if_current(self.store, job, status, seq, self._post))
        self.assertEqual(self.posted, ["drain posted lease lost"])

    def test_a_verdict_whose_post_failed_is_owed_again(self) -> None:
        job = self.store.enqueue("org/app", "sha1", "main")
        self._lapse(job.id, 100.0, "lease lost")
        _owed, status, seq = self.store.claim_owed_statuses()[0]

        def refuse(job_, state, description):
            raise GitHubAPIError("GitHub is not answering")

        self.assertFalse(post_if_current(self.store, job, status, seq, refuse))
        owed = self.store.claim_owed_statuses()
        self.assertEqual([text for _job, text, _seq in owed], [status])

    def test_a_verdict_cannot_be_written_between_the_check_and_the_post(self) -> None:
        # The gap left when the check and the post are two steps: a newer verdict
        # is written while the post is under way, and the post already in flight
        # is then the one that lands last. Everything that writes a verdict takes
        # the same lock, so the writer waits for the post to finish.
        job = self.store.enqueue("org/app", "sha1", "main")
        self._lapse(job.id, 100.0, "lease lost")
        _owed, status, seq = self.store.claim_owed_statuses()[0]
        order: list[str] = []
        posting = threading.Event()

        def writer() -> None:
            # released only once the post is under way, so the two genuinely
            # overlap rather than whichever the scheduler picked
            posting.wait(5)
            self.store.reclaim_expired(now=400.0, publish=lambda j: json.dumps(["error", "x"]))
            order.append("writer wrote a verdict")

        def publish(job_, state, description) -> None:
            order.append("post started")
            posting.set()
            time.sleep(0.2)
            order.append("post finished")

        worker = threading.Thread(target=writer)
        worker.start()
        self.assertTrue(post_if_current(self.store, job, status, seq, publish))
        worker.join(10)
        self.assertEqual(
            order,
            ["post started", "post finished", "writer wrote a verdict"],
            "a verdict was written while a post was in flight",
        )

    def test_a_loss_claimed_for_posting_is_dropped_when_the_run_completes(self) -> None:
        # A lease lapses, the drain takes the loss, and the device comes back and
        # finishes the run before the post goes out. The loss is no longer the
        # run's state, and posting it would report a run that passed as lost.
        job = self.store.enqueue("org/app", "sha1", "main")
        self._lapse(job.id, 100.0, "lease lost")
        _owed, loss, loss_seq = self.store.claim_owed_statuses()[0]

        self.store.claim("device-alpha", [], lease_seconds=600, now=100_000.0)
        reported = self.store.report(job.id, "device-alpha", 0, now=100_001.0)
        self.assertEqual(reported.status, SUCCESS)
        self.assertNotEqual(self.store.status_seq(job.id), loss_seq)

        sent = post_if_current(self.store, job, loss, loss_seq, self._post)
        self.assertFalse(sent, "a loss was posted after the run passed")
        self.assertEqual(self.posted, [])

    def test_a_completed_run_owes_nothing(self) -> None:
        # the loss the reclaim wrote is not left in the queue for the next drain
        job = self.store.enqueue("org/app", "sha1", "main")
        self._lapse(job.id, 100.0, "lease lost")
        self.assertEqual(len(self.store.claim_owed_statuses()), 1)
        self.store.claim("device-alpha", [], lease_seconds=600, now=100_000.0)
        self.store.report(job.id, "device-alpha", 0, now=100_001.0)
        self.assertEqual(self.store.claim_owed_statuses(), [])

    def test_every_verdict_advances_the_sequence(self) -> None:
        job = self.store.enqueue("org/app", "sha1", "main")
        first = self.store.status_seq(job.id)
        self._lapse(job.id, 100.0, "lease lost")
        self.assertEqual(self.store.status_seq(job.id), first + 1)
        self.store.claim_owed_statuses()
        self._lapse(job.id, 100_000.0, "lease lost again")
        self.assertEqual(self.store.status_seq(job.id), first + 2)

    def test_a_run_that_owes_nothing_has_the_base_sequence(self) -> None:
        job = self.store.enqueue("org/app", "sha1", "main")
        self.assertEqual(self.store.status_seq(job.id), 0)



if __name__ == "__main__":
    unittest.main()

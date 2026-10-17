"""A run's check run reaches GitHub from the broker, and a refusal never costs a run.

Split out of test_check_runs and test_jobapi rather than appended to them: both
were within a few lines of the test ceiling, and these two classes are about one
subject, which is the publication itself rather than the client behind it.
"""
import contextlib
import json
import tempfile
import time
import threading
import unittest
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from cindral.check_runs import CheckRunAPIError, CheckRunPublisher, publish_check_run_quietly
from cindral.github import GitHubClient
from cindral.github import GitHubAPIError
from cindral import jobs as cindral_jobs
from cindral.jobs import FAILURE, SUCCESS, JobStore
from cindral.lease_reclaim import DEAD, PENDING, RUNNING
from cindral.deliveries import DeliveryStore
from cindral.policy import Policy, load_dispatch_targets
from cindral.service import WEBHOOK_PATH, CindralHandler, CindralServer
from cindral.webhook import sign
from job_endpoint_harness import JobEndpointHarness

REPO = "example-org/example-app"
SHA = "a" * 40
INSTALL = "corepack enable && pnpm install --frozen-lockfile"
SECRET = "whsec_" + "d" * 32


def job(**overrides):
    from cindral.jobrecord import Job
    from cindral.lease_reclaim import RUNNING

    base = Job(
        id="job-1",
        repository=REPO,
        sha=SHA,
        ref="refs/heads/main",
        labels=(),
        timeout=600,
        created_at=1_750_000_000.0,
        status=RUNNING,
        device="desktop",
        attempts=1,
    )
    return replace(base, **overrides) if overrides else base


class Response:
    def __init__(self, body: bytes = b"{}"):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


def refused(code: int, message: str = "refused"):
    from urllib.error import HTTPError

    return HTTPError("url", code, message, {}, None)


class QuietPublishTest(unittest.TestCase):
    """The wrapper every call site uses, so a refusal cannot take a run down."""

    def test_it_reports_a_landed_publish(self) -> None:
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(b'{"check_runs": []}'), Response(b'{"id": 7}')]
            self.assertTrue(publish_check_run_quietly(GitHubClient("token"), job()))

    def test_it_absorbs_a_refusal(self) -> None:
        # the whole point: the commit status is the retry surface, so a check
        # GitHub refuses must not propagate into the request that reported the run
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(b'{"check_runs": []}'), refused(403, "forbidden")]
            self.assertFalse(publish_check_run_quietly(GitHubClient("token"), job()))

    def test_it_absorbs_a_job_it_cannot_describe(self) -> None:
        # a repository slug the checks API cannot accept is a mistake in the
        # request, not a reason to fail the run that reported it
        self.assertFalse(publish_check_run_quietly(GitHubClient("token"), job(repository="not-a-slug")))

    def test_no_client_is_a_no_op_rather_than_an_error(self) -> None:
        self.assertFalse(publish_check_run_quietly(None, job()))


class CheckRunPublishedTest(JobEndpointHarness, unittest.TestCase):
    """A device run must reach the commit as a check run, not only a status.

    The call site is spied rather than the publisher mocked out, so what is
    asserted is that the handler asks for a check to be published. What that
    publish does with a refusal is the wrapper's own contract above, so the two
    halves are pinned separately instead of one mock standing in for both.
    """

    def test_a_report_publishes_the_check_run(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        with patch("cindral.jobapi.publish_check_run_quietly") as publish:
            status, _ = self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "server", "exit_code": 0})
        self.assertEqual(status, 200)
        publish.assert_called_once()
        self.assertEqual(publish.call_args[0][1].id, job.id)

    def test_the_check_is_published_from_the_recorded_verdict(self) -> None:
        # the job handed to the publisher must already carry the outcome, or the
        # check closes while GitHub still renders it in progress
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        with patch("cindral.jobapi.publish_check_run_quietly") as publish:
            self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "server", "exit_code": 1})
        self.assertEqual(publish.call_args[0][1].status, FAILURE)

    def test_a_refused_check_does_not_fail_the_report(self) -> None:
        # driven through the real wrapper, not a mocked one: the refusal has to be
        # absorbed on the way out, because the verdict is already recorded by the
        # time the check is published, so a raise here would answer 500 to a
        # device whose run actually finished
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        with patch.object(self.server, "github"), patch(
            "cindral.check_runs.CheckRunPublisher.publish",
            side_effect=CheckRunAPIError("github refused", status_code=403, retry_after=None),
        ):
            status, payload = self.call(
                "POST", f"/v1/jobs/{job.id}/report", {"device": "server", "exit_code": 0}
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "success")

    def test_a_broker_without_a_github_client_still_reports(self) -> None:
        # the wrapper is a no-op rather than an error when there is no client, so
        # a broker configured without one keeps answering devices
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        self.assertIsNone(self.server.github)
        status, _ = self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "server", "exit_code": 0})
        self.assertEqual(status, 200)


class QueuedCheckPublishedTest(unittest.TestCase):
    """A queued run must open its check, or the commit shows nothing until it ends.

    Driven over a real socket against a repository in device mode, because the
    enqueue path is a webhook handler and asserting on a source string would only
    prove the word is present.
    """

    def setUp(self) -> None:
        self._db = tempfile.TemporaryDirectory()
        self.addCleanup(self._db.cleanup)
        self.server = CindralServer(("127.0.0.1", 0), CindralHandler)
        self.server.deliveries = DeliveryStore(str(Path(self._db.name) / "deliveries.db"))
        self.server.jobs = JobStore(str(Path(self._db.name) / "queue.db"))
        self.server.agent_token = "agent-token"
        self.server.policy = replace(
            Policy.load("config/policy.toml"),
            dispatch_targets=load_dispatch_targets(
                {"dispatch": {"repositories": [{"mode": "device", "repository": REPO}]}}
            ),
        )
        self.server.runners = ()
        self.server.github = None
        self.server.webhook_secret = SECRET
        self.server.workflow_file = "cindral-dispatch.yml"
        self.server.capacity_lock = threading.Lock()
        self.server.runner_reservations = {}
        self.server.reservation_seconds = 10
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        self.port = self.server.server_address[1]
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def push(self) -> tuple[int, dict]:
        body = json.dumps(
            {
                "ref": "refs/heads/main",
                "after": SHA,
                "deleted": False,
                "repository": {"full_name": REPO, "private": True, "default_branch": "main"},
            }
        ).encode()
        headers = {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"}
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", WEBHOOK_PATH, body=body, headers={"Content-Type": "application/json", **headers})
        response = conn.getresponse()
        payload = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, payload

    def test_a_queued_run_publishes_its_check(self) -> None:
        with patch("cindral.service.publish_check_run_quietly") as publish:
            status, _ = self.push()
        # 202, not 200: the run is queued, not finished, and the check is what
        # makes that visible on the commit in the meantime
        self.assertEqual(status, 202)
        publish.assert_called_once()
        self.assertEqual(publish.call_args[0][1].repository, REPO)

    def test_a_refused_check_still_queues_the_run(self) -> None:
        # the run is the point; a check that cannot be written must not cost it
        with patch("cindral.service.publish_check_run_quietly", return_value=False):
            status, _ = self.push()
        self.assertEqual(status, 202)
        self.assertEqual(self.server.jobs.list()[0].repository, REPO)


class OwedCheckPublishedTest(JobEndpointHarness, unittest.TestCase):
    """A check whose write failed is owed, and the drain is where it converges.

    M3 showed this call site had no test: removing it left the suite green. The
    window is real, since the commit status and the check are written by two
    separate requests and only one of them can fail on its own.
    """

    def owed_run(self):
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("burst", [], lease_seconds=1, now=time.time() - 10)
        with patch.object(self.server, "github") as github:
            github.post_status.side_effect = GitHubAPIError("temporary")
            self.call("POST", "/v1/jobs/claim", {"device": "server"})
        self.assertEqual(len(self.server.jobs.owed_statuses()), 1)
        return job

    def test_draining_an_owed_status_publishes_the_check(self) -> None:
        job = self.owed_run()
        with patch.object(self.server, "github") as github, patch(
            "cindral.jobapi.publish_check_run_quietly"
        ) as publish:
            github.post_status.side_effect = None
            github.post_status.return_value = None
            self.call("POST", "/v1/jobs/claim", {"device": "server"})
        publish.assert_called()
        self.assertEqual(publish.call_args[0][1].id, job.id)

    def test_the_check_publishes_from_the_stored_verdict_not_the_owed_one(self) -> None:
        # the queue holds the loss; the stored job is what the check should show,
        # and publishing the owed tuple would put a stale "lost" on a run that
        # has since been reclaimed and re-run
        self.owed_run()
        with patch.object(self.server, "github") as github, patch(
            "cindral.jobapi.publish_check_run_quietly"
        ) as publish:
            github.post_status.side_effect = None
            github.post_status.return_value = None
            self.call("POST", "/v1/jobs/claim", {"device": "server"})
        published = publish.call_args[0][1]
        # The concrete value, not membership in the legal set: asserting that
        # passes for the stale loss this test exists to rule out, since DEAD is
        # one of the legal statuses and is exactly the wrong one here.
        self.assertEqual(published.status, PENDING)


class RefusalReachesNoRequestTest(JobEndpointHarness, unittest.TestCase):
    """Every way the producer can fail must stop at the wrapper, not escape it.

    `CheckRunAPIError` is only the subclass the HTTP layer raises. The producer
    also raises the bare `GitHubAPIError` when GitHub answers with something it
    cannot read, and an exception that escapes the wrapper escapes the request
    handler with it, so the device gets no response at all rather than a 4xx.
    The drain is the worst case: it runs before `store.claim`, so one unreadable
    body blocks every device's claim and never clears the owed row.
    """

    def unreadable(self) -> list[dict]:
        """The producer's own unreadable-answer paths, as urlopen side effects."""
        return [
            {"body": b"<html>not json</html>"},
            {"body": b'["a list, not an object"]'},
            {"body": b'{"check_runs": "not a list"}'},
        ]

    def publish(self, responses: list[dict]):
        from cindral import check_runs
        from cindral.check_runs import publish_check_run_quietly
        from cindral.github import GitHubClient
        from cindral.jobrecord import Job
        from cindral.lease_reclaim import RUNNING

        job = Job(
            id="job-1",
            repository=REPO,
            sha="a" * 40,
            ref="refs/heads/main",
            labels=(),
            timeout=600,
            created_at=1_750_000_000.0,
            status=RUNNING,
            device="desktop",
            attempts=1,
        )
        bodies = [r["body"] for r in responses]
        with patch("cindral.check_runs.urlopen") as opened:
            opened.side_effect = [Response(bodies[0]), *[Response(b) for b in bodies[1:]]]
            return publish_check_run_quietly(GitHubClient("token"), job)

    def test_an_unreadable_find_response_is_absorbed(self) -> None:
        for body in (b"<html>not json</html>", b'["a list"]', b'{"check_runs": "nope"}'):
            with self.subTest(body=body):
                self.assertFalse(self.publish([{"body": body}]))

    def test_a_create_that_answers_without_an_id_is_absorbed(self) -> None:
        # the 200 arrives, the id does not, and the base error is what is raised
        self.assertFalse(self.publish([{"body": b'{"check_runs": []}'}, {"body": b'{"ok": true}'}]))

    def test_a_find_entry_without_a_numeric_id_is_absorbed(self) -> None:
        self.assertFalse(
            self.publish([{"body": b'{"check_runs": [{"id": "not-a-number"}]}'}, {"body": b"{}"}])
        )

    def test_a_claim_survives_an_unreadable_owed_verdict(self) -> None:
        # the end-to-end consequence: the drain runs before store.claim, so an
        # escaping exception here would wedge the endpoint for every device
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("burst", [], lease_seconds=1, now=time.time() - 10)
        with patch.object(self.server, "github") as github:
            github.post_status.side_effect = GitHubAPIError("temporary")
            self.call("POST", "/v1/jobs/claim", {"device": "server"})
        self.assertEqual(len(self.server.jobs.owed_statuses()), 1)
        with patch.object(self.server, "github") as github, patch(
            "cindral.check_runs.urlopen"
        ) as opened:
            github.post_status.side_effect = None
            github.post_status.return_value = None
            opened.side_effect = [Response(b"<html>not json</html>")]
            status, _ = self.call("POST", "/v1/jobs/claim", {"device": "device-b"})
        self.assertIn(status, (200, 204), "the claim endpoint answered the device")
        # the row stays owed, because the check never landed. The commit status
        # is safe to repost and the check is not, so this is the cheap failure.
        self.assertEqual(len(self.server.jobs.owed_statuses()), 1)

    def _owe_a_loss(self) -> None:
        self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("burst", [], lease_seconds=1, now=time.time() - 10)
        with patch.object(self.server, "github") as github:
            github.post_status.side_effect = GitHubAPIError("temporary")
            self.call("POST", "/v1/jobs/claim", {"device": "server"})
        self.assertEqual(len(self.server.jobs.owed_statuses()), 1)

    def test_a_refused_check_leaves_the_row_owed_for_the_next_drain(self) -> None:
        # Clearing the row regardless would make a missing check permanent, and
        # the check is the feature. The cost is one duplicate commit status on
        # the retry, which is the cheaper of the two failures.
        self._owe_a_loss()
        with patch.object(self.server, "github") as github, patch(
            "cindral.jobapi.publish_check_run_quietly", return_value=False
        ):
            github.post_status.side_effect = None
            github.post_status.return_value = None
            self.call("POST", "/v1/jobs/claim", {"device": "device-b"})
        self.assertEqual(len(self.server.jobs.owed_statuses()), 1, "a refused check was retired anyway")
        with patch.object(self.server, "github") as github, patch(
            "cindral.jobapi.publish_check_run_quietly", return_value=True
        ):
            github.post_status.side_effect = None
            github.post_status.return_value = None
            self.call("POST", "/v1/jobs/claim", {"device": "device-c"})
        self.assertEqual(self.server.jobs.owed_statuses(), [])

    def test_an_unexpected_error_leaves_the_row_owed_and_the_endpoint_recovers(self) -> None:
        # the counterpart: a bug propagates, and the row survives so the retry can
        # clear it. The endpoint still answers, because a drain that cannot
        # retire its row would wedge every device rather than one job.
        self._owe_a_loss()
        with patch.object(self.server, "github") as github, patch(
            "cindral.jobapi.publish_check_run_quietly", side_effect=RuntimeError("bug")
        ):
            github.post_status.side_effect = None
            github.post_status.return_value = None
            with contextlib.suppress(Exception):
                self.call("POST", "/v1/jobs/claim", {"device": "device-b"})
        with patch.object(self.server, "github") as github, patch(
            "cindral.jobapi.publish_check_run_quietly", return_value=True
        ):
            github.post_status.side_effect = None
            github.post_status.return_value = None
            status, _ = self.call("POST", "/v1/jobs/claim", {"device": "device-c"})
        self.assertIn(status, (200, 204))
        self.assertEqual(self.server.jobs.owed_statuses(), [])


class CheckNameIsTheDefaultTest(unittest.TestCase):
    """Every call site must publish under the name the producer defines.

    The producer keys its cached check ids by name, so publishing a run under a
    second name opens a second check run for the same job and leaves the first
    one pending on the commit forever. The producer's own tests pin
    `DEFAULT_CHECK_NAME`, and none of that reaches a call site, so a call site
    that names its own check is invisible.
    """

    def call_sites(self) -> list[tuple[str, str]]:
        import pathlib

        import cindral

        root = pathlib.Path(cindral.__file__).parent
        return [
            (path.name, path.read_text(encoding="utf-8"))
            for path in root.glob("*.py")
            if path.name != "check_runs.py" and "publish_check_run_quietly(" in path.read_text(encoding="utf-8")
        ]

    def test_no_call_site_overrides_the_check_name(self) -> None:
        import re

        for module, source in self.call_sites():
            with self.subTest(module=module):
                # only the call's own arguments, not every `name=` in the file
                for call in re.findall(r"publish_check_run_quietly\((?:[^()]|\([^()]*\))*\)", source, re.S):
                    self.assertNotIn("name=", call, f"a call site names its own check: {call}")

    def test_every_call_site_relies_on_the_producer_default(self) -> None:
        from cindral.check_runs import DEFAULT_CHECK_NAME

        for module, source in self.call_sites():
            with self.subTest(module=module):
                self.assertIn("publish_check_run_quietly(", source)
        self.assertEqual(DEFAULT_CHECK_NAME, "cindral/ci")
class CompletedCheckStandsTest(unittest.TestCase):
    """A check GitHub has finished must not be reopened by a stale in-flight job.

    A job the queue has released for another device is `pending`, and a pending
    job has no conclusion. Publishing that over a completed check moves it back
    to in progress on GitHub, so the commit shows a run that never finishes. The
    check GitHub holds is the later truth, so it stands and the write is skipped.
    """

    def terminal(self):
        """A finished run: a conclusion, so the check it opens is completed."""
        return job(status=cindral_jobs.FAILURE, exit_code=1, log="boom")

    def publish_pending_against_a_completed_check(self) -> tuple[int, int]:
        """A run that finished, was then released for another device, and is pending."""
        publisher = CheckRunPublisher(GitHubClient("token"))
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [
                Response(b'{"check_runs": []}'),
                Response(b'{"id": 42}'),
                Response(b"{}"),
            ]
            publisher.publish(self.terminal())  # opens and finishes the check red
            published = publisher.publish(job(status=PENDING))
        return urlopen.call_count, published.check_run_id

    def test_a_pending_verdict_does_not_reopen_a_completed_check(self) -> None:
        # two requests: the find that opens the check and the create that
        # finishes it. The pending publish writes nothing, because a completed
        # check is the later truth and reopening it leaves the run unfinished on
        # a commit that already said otherwise.
        calls, check_run_id = self.publish_pending_against_a_completed_check()
        self.assertEqual(calls, 2, "a completed check was updated back to in progress")
        self.assertEqual(check_run_id, 42)

    def test_a_terminal_verdict_still_updates_a_completed_check(self) -> None:
        # the guard is on the conclusion being absent, not on the check being
        # finished: a later real verdict is newer information and must land
        publisher = CheckRunPublisher(GitHubClient("token"))
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [
                Response(b'{"check_runs": []}'),
                Response(b'{"id": 42}'),
                Response(b"{}"),
            ]
            publisher.publish(self.terminal())  # opens and finishes the check red
            # a later, different verdict is newer information and must land
            publisher.publish(job(status=SUCCESS, exit_code=0, log="ok"))
        update = json.loads(urlopen.call_args_list[-1].args[0].data)
        self.assertEqual(update["conclusion"], "success")

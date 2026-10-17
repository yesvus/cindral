import json
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from cindral import jobs
from cindral.github import MAX_STATUS_DESCRIPTION, GitHubAPIError
from cindral.status_text import failure_description, lost_description
from cindral.retry import LEASE_LOST, MAX_ATTEMPTS, MAX_BACKOFF_SECONDS
from cindral.webhook import sign

REPO = "example-org/example-app"
STATUS_TARGET = "https://panel.example.com/api/jobs/{job_id}"


def push_body(private=True):
    return json.dumps(
        {
            "ref": "refs/heads/main",
            "after": "abc123",
            "repository": {"full_name": REPO, "private": private, "default_branch": "main"},
        }
    ).encode()


def pull_request_body(head_repo=REPO, action="opened", number=42, author_association="OWNER"):
    # author_association belongs inside the pull_request object; the webhook
    # parser reads the trust decision from there, so a top-level copy makes
    # every request look association-less
    return json.dumps(
        {
            "action": action,
            "number": number,
            "repository": {"full_name": REPO, "private": True, "default_branch": "main"},
            "pull_request": {
                "author_association": author_association,
                "head": {"sha": "head123", "repo": {"full_name": head_repo}},
                "merge_commit_sha": "merge123",
            },
        }
    ).encode()


# the harness is a sibling module, reached the same way its other importers
# reach theirs, which is by test discovery putting this directory on the path
from job_endpoint_harness import (
    AGENT_TOKEN,
    POOL_TOKEN,
    SECRET,
    JobEndpointHarness,
)


#: a merge sha of the shape git produces, so the shape check cannot be what
#: rejects it and the parent check is the only thing that can
MERGE_SHA = "b" * 40


class JobEndpointTest(JobEndpointHarness, unittest.TestCase):
    def test_claim_requires_a_bearer_token(self) -> None:
        status, _ = self.call("POST", "/v1/jobs/claim", {"device": "server"}, authorize=False)
        self.assertEqual(status, 401)

    def test_claim_returns_no_content_when_queue_is_empty(self) -> None:
        status, payload = self.call("POST", "/v1/jobs/claim", {"device": "server"})
        self.assertEqual(status, 204)
        self.assertEqual(payload, {})

    def test_claim_and_report_round_trip(self) -> None:
        enqueued = self.server.jobs.enqueue(REPO, "abc123", "main", labels=("arm64",))
        status, payload = self.call("POST", "/v1/jobs/claim", {"device": "server", "labels": ["arm64"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["job"]["id"], enqueued.id)
        self.assertEqual(payload["job"]["sha"], "abc123")
        status, payload = self.call("POST", f"/v1/jobs/{enqueued.id}/report", {"device": "server", "exit_code": 0})
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "success")
        self.assertFalse(payload["status_posted"])

    def test_reported_steps_are_stored_and_served_on_the_job(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        steps = [
            {"index": 1, "name": "pnpm install", "output": "added 412 packages", "completed": True},
            {"index": 2, "name": "pnpm run ci", "output": "assertion failed", "completed": False},
        ]
        status, payload = self.call(
            "POST", f"/v1/jobs/{job.id}/report",
            {"device": "server", "exit_code": 1, "log": "cindral: step 2: pnpm run ci\n", "steps": steps},
        )
        self.assertEqual(status, 200)
        self.assertEqual([step["name"] for step in payload["steps"]], ["pnpm install", "pnpm run ci"])
        self.assertFalse(payload["steps"][1]["completed"])
        status, served = self.call("GET", f"/v1/jobs/{job.id}")
        self.assertEqual(served["steps"][1]["output"], "assertion failed")

    def test_a_report_without_steps_still_reads_as_no_steps(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        status, payload = self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "server", "exit_code": 1})
        self.assertEqual(status, 200)
        self.assertEqual(payload["steps"], [])

    def test_a_malformed_step_in_a_report_is_dropped_not_stored(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        status, payload = self.call(
            "POST", f"/v1/jobs/{job.id}/report",
            {"device": "server", "exit_code": 1, "steps": ["a string", {"name": "no index"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["steps"], [])

    def test_claim_skips_jobs_the_device_cannot_match(self) -> None:
        self.server.jobs.enqueue(REPO, "abc123", "main", labels=("arm64",))
        status, _ = self.call("POST", "/v1/jobs/claim", {"device": "server", "labels": ["x64"]})
        self.assertEqual(status, 204)

    def test_renew_rejects_the_wrong_device(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        status, _ = self.call("POST", f"/v1/jobs/{job.id}/renew", {"device": "burst"})
        self.assertEqual(status, 409)
        status, payload = self.call("POST", f"/v1/jobs/{job.id}/renew", {"device": "server"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["renewed"])

    def test_report_rejects_a_job_leased_to_another_device(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        status, _ = self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "burst", "exit_code": 0})
        self.assertEqual(status, 409)

    def test_report_posts_a_failure_commit_status(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        with patch.object(self.server, "github") as github:
            status, payload = self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "server", "exit_code": 7})
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "failure")
        self.assertTrue(payload["status_posted"])
        repository, sha, state = github.post_status.call_args[0][:3]
        self.assertEqual((repository, sha, state), (REPO, "abc123", "failure"))

    def test_successful_report_posts_a_success_commit_status(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        with patch.object(self.server, "github") as github:
            _, payload = self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "server", "exit_code": 0})
        self.assertTrue(payload["status_posted"])
        self.assertEqual(github.post_status.call_args[0][2], "success")

    def test_failure_status_names_the_failing_step(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        log = (
            "cindral: step 1: pip install --quiet -e .\n"
            "Successfully installed cindral-0.10.0\n"
            "cindral: step 2: python -m unittest discover -s tests\n"
            "FAILED (failures=1)\n"
        )
        with patch.object(self.server, "github") as github:
            self.call(
                "POST",
                f"/v1/jobs/{job.id}/report",
                {"device": "server", "exit_code": 1, "log": log},
            )
        description = github.post_status.call_args[0][3]
        self.assertEqual(
            description,
            "device pool run failed (exit 1): step 2: python -m unittest discover -s tests",
        )
        # step output can hold anything the run printed, so it stays in the log
        self.assertNotIn("FAILED", description)

    def test_failure_status_links_to_the_job_view(self) -> None:
        self.server.status_target_url = STATUS_TARGET
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        with patch.object(self.server, "github") as github:
            self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "server", "exit_code": 7})
        self.assertEqual(
            github.post_status.call_args.kwargs["target_url"],
            f"https://panel.example.com/api/jobs/{job.id}",
        )

    def test_status_omits_the_link_when_no_view_is_configured(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        with patch.object(self.server, "github") as github:
            self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "server", "exit_code": 1})
        self.assertEqual(github.post_status.call_args.kwargs["target_url"], "")

    def test_queued_status_links_to_the_job_view(self) -> None:
        self.server.status_target_url = STATUS_TARGET
        body = push_body()
        with patch.object(self.server, "github") as github:
            status, _ = self.deliver(
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"},
            )
        self.assertEqual(status, 202)
        queued = self.server.jobs.list()[0]
        self.assertEqual(
            github.post_status.call_args.kwargs["target_url"],
            f"https://panel.example.com/api/jobs/{queued.id}",
        )

    def test_a_device_error_posts_a_loss_and_queues_the_run_again(self) -> None:
        # a device that died leaves no verdict, so the commit reads as a lost run
        # to retry rather than the red failed test it used to be indistinguishable from
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        with patch.object(self.server, "github") as github:
            status, payload = self.call(
                "POST",
                f"/v1/jobs/{job.id}/report",
                {"device": "server", "error": "docker is not running"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["attempts"], 1)
        self.assertIsNone(payload["exit_code"])
        self.assertEqual(github.post_status.call_args[0][2], "pending")
        self.assertEqual(
            github.post_status.call_args[0][3],
            "device pool run lost, retry 2 of 3: docker is not running",
        )
        self.assertEqual(self.server.jobs.get(job.id).status, "pending")

    def test_a_lost_run_reads_differently_from_a_failed_one(self) -> None:
        # reporting exit 1 for a device that died made the loss byte for byte
        # the failed-test run, so the commit could not be acted on either way
        self.server.jobs.enqueue(REPO, "abc123", "main")
        claimed = self.server.jobs.claim("server", [])
        state, description = lost_description(claimed, "docker is not running")
        self.assertEqual(state, "pending")
        self.assertNotEqual(description, failure_description(1, ""))
        self.assertIn("lost", description)
        self.assertIn("docker is not running", description)
        self.assertNotIn("exit", description)

    def test_a_loss_description_fits_a_commit_status(self) -> None:
        # a device error is bounded at MAX_ERROR_CHARS, which is larger than the
        # 140 characters a status allows, so the text has to be bounded here too
        # the way failure_description is
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        claimed = self.server.jobs.claim("server", [])
        long_error = "docker is not running and the device also lost its disk and more"
        for attempts, state in ((1, "pending"), (MAX_ATTEMPTS, "error")):
            actual, description = lost_description(replace(claimed, attempts=attempts), long_error)
            self.assertEqual(actual, state)
            self.assertLessEqual(len(description), MAX_STATUS_DESCRIPTION)

    def test_a_device_error_past_the_attempt_cap_parks_the_run(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.lose_attempts(job, MAX_ATTEMPTS - 1)
        self.server.jobs.claim("server", [], lease_seconds=300)
        with patch.object(self.server, "github") as github:
            status, payload = self.call(
                "POST",
                f"/v1/jobs/{job.id}/report",
                {"device": "server", "error": "docker is not running"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "dead")
        self.assertEqual(payload["attempts"], MAX_ATTEMPTS)
        # an infrastructure failure is not a failed test, so the commit says error
        self.assertEqual(github.post_status.call_args[0][2], "error")
        self.assertIn(f"after {MAX_ATTEMPTS} attempts", github.post_status.call_args[0][3])
        self.assertIsNone(self.server.jobs.claim("server", []))

    def test_a_report_with_neither_a_verdict_nor_an_error_is_refused(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        status, payload = self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "server"})
        self.assertEqual(status, 400)
        self.assertIn("exit code", payload["error"])
        self.assertEqual(self.server.jobs.get(job.id).status, "running")

    def test_a_report_whose_exit_code_is_null_is_refused(self) -> None:
        # an explicit null is the same absence as omitting the key, and must be
        # refused with the same message rather than reaching int(None)
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        status, payload = self.call(
            "POST", f"/v1/jobs/{job.id}/report", {"device": "server", "exit_code": None}
        )
        self.assertEqual(status, 400)
        self.assertIn("exit code", payload["error"])
        self.assertNotIn("NoneType", payload["error"])
        self.assertEqual(self.server.jobs.get(job.id).status, "running")

    def test_a_lapsed_lease_posts_a_loss_when_the_next_device_claims(self) -> None:
        # nothing reported the run that died with its device, so the broker is
        # the only thing left that can tell the commit what happened
        self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("burst", [], lease_seconds=1, now=time.time() - 10)
        with patch.object(self.server, "github") as github:
            status, _ = self.call("POST", "/v1/jobs/claim", {"device": "server"})
        self.assertEqual(status, 204)
        self.assertEqual(github.post_status.call_args[0][2], "pending")
        self.assertIn("lost", github.post_status.call_args[0][3])
        self.assertIn(LEASE_LOST, github.post_status.call_args[0][3])

    def test_a_loss_status_that_cannot_publish_is_retried_by_a_later_claim(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("burst", [], lease_seconds=1, now=time.time() - 10)
        with patch.object(self.server, "github") as github, patch(
            "cindral.jobapi.publish_check_run_quietly", return_value=True
        ) as publish:
            github.post_status.side_effect = GitHubAPIError("temporary")
            self.call("POST", "/v1/jobs/claim", {"device": "server"})
            self.assertIsNotNone(self.server.jobs.owed_statuses())
            # the same verdict is still owed, so a later queue event republishes
            # it rather than leaving the commit on its previous status
            github.post_status.side_effect = None
            self.call("POST", "/v1/jobs/claim", {"device": "server"})
        publish.assert_called()
        self.assertIn(LEASE_LOST, github.post_status.call_args[0][3])
        self.assertEqual(self.server.jobs.owed_statuses(), [])

    def test_a_published_loss_status_is_not_republished(self) -> None:
        self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("burst", [], lease_seconds=1, now=time.time() - 10)
        # the check publish is stubbed to succeed, because this is about the
        # commit status. Left real, it runs against the MagicMock github client
        # and its network call fails, so the drain would retry the row and the
        # assertion would be about the stub rather than about the status.
        with patch.object(self.server, "github") as github, patch(
            "cindral.jobapi.publish_check_run_quietly", return_value=True
        ):
            self.call("POST", "/v1/jobs/claim", {"device": "server"})
            self.call("POST", "/v1/jobs/claim", {"device": "server"})
        self.assertEqual(github.post_status.call_count, 1)

    def test_a_later_report_supersedes_a_loss_status_still_owed(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("burst", [], lease_seconds=1, now=time.time() - 10)
        with patch.object(self.server, "github") as github:
            github.post_status.side_effect = GitHubAPIError("temporary")
            self.call("POST", "/v1/jobs/claim", {"device": "server"})
            self.assertEqual(len(self.server.jobs.owed_statuses()), 1)
            github.post_status.side_effect = None
            # a retry that actually claims the run publishes its own verdict,
            # which supersedes the loss the queue was still holding
            self.server.jobs.claim("server", [], now=time.time() + MAX_BACKOFF_SECONDS + 60)
            status, _ = self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "server", "exit_code": 0})
        self.assertEqual(status, 200)
        self.assertEqual(self.server.jobs.owed_statuses(), [])

    def test_a_run_out_of_attempts_posts_an_error_status_instead_of_staying_queued(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        base = time.time()
        for index in range(MAX_ATTEMPTS - 1):
            at = base - (MAX_ATTEMPTS - index) * (MAX_BACKOFF_SECONDS + 60)
            self.server.jobs.claim("burst", [], lease_seconds=1, now=at)
            self.server.jobs.reclaim_expired(now=at + 2)
        # the last attempt's lease lapsed already, so the next claim is what
        # notices the loss and parks the run
        self.server.jobs.claim("burst", [], lease_seconds=1, now=base - 10)
        with patch.object(self.server, "github") as github:
            status, _ = self.call("POST", "/v1/jobs/claim", {"device": "server"})
        self.assertEqual(status, 204)
        self.assertEqual(github.post_status.call_args[0][2], "error")
        self.assertEqual(self.server.jobs.get(job.id).status, "dead")
        self.assertEqual(self.server.jobs.list(jobs.DEAD)[0].attempts, MAX_ATTEMPTS)

    def test_a_lost_run_exposes_its_reason_and_attempts(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [], lease_seconds=300)
        self.call(
            "POST",
            f"/v1/jobs/{job.id}/report",
            {"device": "server", "error": "docker is not running"},
        )
        status, payload = self.call("GET", f"/v1/jobs/{job.id}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["last_error"], "docker is not running")
        self.assertEqual(payload["attempts"], 1)
        self.assertIsNone(payload["exit_code"])

    def test_job_log_serves_the_reported_tail(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        log = "cindral: step 1: pnpm run ci\nassertion failed\n"
        self.call(
            "POST",
            f"/v1/jobs/{job.id}/report",
            {"device": "server", "exit_code": 1, "log": log},
        )
        status, body, content_type, cors = self.fetch("GET", f"/v1/jobs/{job.id}/log", POOL_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body, log)
        self.assertEqual(content_type, "text/plain; charset=utf-8")
        # a page a browser reaches must not be able to read a run log
        self.assertIsNone(cors)

    def test_job_log_returns_empty_when_no_log_was_reported(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        status, body, content_type, _ = self.fetch("GET", f"/v1/jobs/{job.id}/log", POOL_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body, "")
        self.assertEqual(content_type, "text/plain; charset=utf-8")

    def test_job_log_refuses_an_unauthenticated_request(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        status, _, _, _ = self.fetch("GET", f"/v1/jobs/{job.id}/log")
        self.assertEqual(status, 401)

    def test_job_log_for_an_unknown_job_is_not_found(self) -> None:
        status, _, _, _ = self.fetch("GET", "/v1/jobs/missing/log", POOL_TOKEN)
        self.assertEqual(status, 404)

    def test_job_log_rejects_post(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        status, _, _, _ = self.fetch("POST", f"/v1/jobs/{job.id}/log", AGENT_TOKEN)
        self.assertEqual(status, 405)
        self.assertEqual(self.server.jobs.get(job.id).status, "running")

    def test_unknown_job_status_is_not_found(self) -> None:
        status, _ = self.call("GET", "/v1/jobs/missing")
        self.assertEqual(status, 404)

    def test_known_job_status_is_returned(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        status, payload = self.call("GET", f"/v1/jobs/{job.id}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "pending")

    def test_push_to_a_direct_repository_queues_instead_of_dispatching(self) -> None:
        body = push_body()
        with patch.object(self.server, "github") as github:
            status, payload = self.deliver(
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"},
            )
        self.assertEqual(status, 202)
        self.assertTrue(payload["queued"])
        self.assertTrue(payload["status_posted"])
        github.dispatch.assert_not_called()
        self.assertEqual(github.post_status.call_args[0][2], "pending")
        queued = self.server.jobs.list()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].sha, "abc123")
        self.assertEqual(queued[0].repository, REPO)

    def test_trusted_pull_request_on_a_direct_repository_queues(self) -> None:
        """The queued commit is the merge the API confirms, not the event's claim.

        `merge_commit_sha` in the payload is computed asynchronously by GitHub, so a
        synchronize event can carry the previous head's merge. The ref is resolved
        here and used only when its parents include this head, so the run builds a
        commit that contains the code under review. A client that cannot answer
        falls back to the head, which is what an unanswered lookup does.
        """
        body = pull_request_body()
        with patch.object(self.server, "github") as github:
            github.pull_request_merge_sha.return_value = (MERGE_SHA, ("a" * 40, "head123"))
            status, payload = self.deliver(
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "pull_request"},
            )
        self.assertEqual(status, 202)
        self.assertTrue(payload["queued"])
        self.assertTrue(payload["status_posted"])
        self.assertEqual(payload["pull_request"], 42)
        self.assertEqual(github.post_status.call_args[0][1], "head123")
        queued = self.server.jobs.list()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].sha, MERGE_SHA)
        self.assertEqual(queued[0].ref, "refs/pull/42/merge")
        self.assertEqual(queued[0].status_sha, "head123")

    def test_fork_pull_request_on_a_direct_repository_stays_on_actions(self) -> None:
        body = pull_request_body(head_repo="someone/example-app")
        with patch.object(self.server, "github") as github:
            github.workflow_exists.return_value = True
            status, payload = self.deliver(
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "pull_request"},
            )
        self.assertEqual(status, 200)
        github.dispatch.assert_called_once()
        self.assertEqual(self.server.jobs.list(), [])

    def test_claim_rejects_get(self) -> None:
        status, _ = self.call("GET", "/v1/jobs/claim")
        self.assertEqual(status, 405)

    def test_status_rejects_post(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        status, _ = self.call("POST", f"/v1/jobs/{job.id}")
        self.assertEqual(status, 405)

    def test_report_retries_after_a_failed_status_update(self) -> None:
        from cindral.github import GitHubAPIError

        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        with patch.object(self.server, "github") as github:
            github.post_status.side_effect = GitHubAPIError("boom")
            status, _ = self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "server", "exit_code": 0})
        # the verdict is recorded even though the status could not be sent, and
        # the status is owed to the next drain. Recording it first is what stops a
        # reclaim releasing the run in between and the work running twice.
        self.assertEqual(status, 200)
        self.assertEqual(self.server.jobs.get(job.id).status, "success")
        owed = self.server.jobs.owed_statuses()
        self.assertEqual(len(owed), 1, "a verdict that could not be posted was dropped")
        self.assertIn("passed", owed[0][1])
        with patch.object(self.server, "github") as github:
            status, payload = self.call(
                "POST", f"/v1/jobs/{job.id}/report", {"device": "server", "exit_code": 0}
            )
        self.assertEqual(status, 409, "a terminal run accepted a second report")

    def test_direct_push_without_agent_token_is_refused(self) -> None:
        self.server.agent_token = None
        body = push_body()
        with patch.object(self.server, "github") as github:
            status, _ = self.deliver(
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"},
            )
        self.assertEqual(status, 503)
        github.dispatch.assert_not_called()
        self.assertEqual(self.server.jobs.list(), [])

    def test_duplicate_delivery_is_idempotent(self) -> None:
        body = push_body()
        headers = {
            "X-Hub-Signature-256": sign(SECRET, body),
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-1",
        }
        with patch.object(self.server, "github"):
            self.deliver(body, headers)
            status, payload = self.deliver(body, headers)
        self.assertEqual(status, 202)
        self.assertTrue(payload["queued"])
        self.assertEqual(len(self.server.jobs.list()), 1)

    def test_job_status_accepts_the_read_only_token(self) -> None:
        # the panel holds only the pool token, so it must be able to read a run
        job = self.server.jobs.enqueue("example-org/app", "sha1", "main", command=("ci",))
        status, payload = self.call("GET", f"/v1/jobs/{job.id}", token=POOL_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(payload["id"], job.id)

    def test_claim_refuses_the_read_only_token(self) -> None:
        self.server.jobs.enqueue("example-org/app", "sha1", "main", command=("ci",))
        status, _ = self.call(
            "POST", "/v1/jobs/claim", {"device": "server"}, token=POOL_TOKEN
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.server.jobs.list(jobs.RUNNING), [])

    def test_report_refuses_the_read_only_token(self) -> None:
        job = self.server.jobs.enqueue("example-org/app", "sha1", "main", command=("ci",))
        self.server.jobs.claim("server", [])
        status, _ = self.call(
            "POST",
            f"/v1/jobs/{job.id}/report",
            {"device": "server", "exit_code": 0},
            token=POOL_TOKEN,
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.server.jobs.get(job.id).status, "running")

    def test_renew_refuses_the_read_only_token(self) -> None:
        job = self.server.jobs.enqueue("example-org/app", "sha1", "main", command=("ci",))
        self.server.jobs.claim("server", [])
        status, _ = self.call(
            "POST", f"/v1/jobs/{job.id}/renew", {"device": "server"}, token=POOL_TOKEN
        )
        self.assertEqual(status, 401)

    def test_heartbeat_requires_a_bearer_token(self) -> None:
        self.assertEqual(self.heartbeat(authorize=False)[0], 401)

    def test_heartbeat_refuses_the_read_only_token(self) -> None:
        # presence is a claim about a device, so a read-only consumer must not
        # be able to make one on the broker's behalf
        self.assertEqual(self.heartbeat(token=POOL_TOKEN)[0], 401)

    def test_heartbeat_records_a_device_the_pool_then_reports(self) -> None:
        # the same lease the queue already knows about counts as active work,
        # so the reported limit never becomes a promise of free slots
        self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        status, payload = self.heartbeat()
        self.assertEqual(status, 200)
        self.assertEqual(payload["device"], "server")
        _, snapshot = self.call("GET", "/v1/pool", token=POOL_TOKEN)
        device = next(d for d in snapshot["devices"] if d["name"] == "server")
        self.assertTrue(device["agent_online"])
        self.assertEqual(device["max_concurrency"], 2)
        self.assertEqual(device["active_jobs"], 1)
        self.assertEqual(device["available_slots"], 1)

    def test_heartbeat_rejects_a_payload_out_of_bounds(self) -> None:
        for body in (
            {"device": "", "labels": [], "max_concurrency": 1},
            {"device": "server", "labels": "arm64", "max_concurrency": 1},
            {"device": "server", "labels": [3], "max_concurrency": 1},
            {"device": "server", "labels": [], "max_concurrency": 0},
            {"device": "server", "labels": [], "max_concurrency": "2"},
            {"device": "server", "labels": [], "max_concurrency": 100000},
        ):
            self.assertEqual(self.heartbeat(body)[0], 400, body)

    def test_heartbeat_rejects_a_body_that_is_not_a_json_object(self) -> None:
        # a JSON value that is not an object carries no device, so it is a bad
        # request rather than a crash the agent cannot interpret
        for body in (b"[]", b'"x"', b"5", b"1.5", b"null", b"true"):
            self.assertEqual(self.heartbeat(body), (400, {"error": "body is not a JSON object"}), body)


if __name__ == "__main__":
    unittest.main()

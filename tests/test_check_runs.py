"""The check run client and the broker's one entry point for a run's check."""
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import cindral
from cindral.check_runs import CheckRunAPIError, CheckRunPublisher, iso8601, publish_check_run
from cindral.check_summary import DEFAULT_CHECK_NAME, summary_for
from cindral.contract import CONTRACT_PATH
from cindral.github import GitHubAPIError, GitHubClient
from cindral.jobrecord import Job, head_sha_for
from cindral.jobs import FAILURE, SUCCESS
from cindral.lease_reclaim import DEAD, PENDING, RUNNING
from cindral.retry import MAX_ATTEMPTS

SHA = "a" * 40
REPO = "example-org/example-app"
INSTALL = "corepack enable && pnpm install --frozen-lockfile"
CHECK = "uv run python scripts/check.py"
CHECKS = "https://api.github.com/repos/example-org/example-app/check-runs"
SECRET = "ghp_" + "c" * 36


def refused(code: int, message: str = "refused", headers=None) -> HTTPError:
    """The refusal urllib raises for a status GitHub answered with."""
    return HTTPError("url", code, message, headers or {}, None)


class Response:
    def __init__(self, body: bytes = b"{}"):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


def job(**overrides) -> Job:
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
    return replace(base, **overrides)


def step(index: int, name: str, *, completed: bool = True, output: str = "") -> dict:
    return {
        "index": index,
        "name": name,
        "output": output,
        "completed": completed,
        "truncated": False,
        "dropped": 0,
    }


def failing(**overrides) -> Job:
    defaults = {
        "status": FAILURE,
        "exit_code": 1,
        "steps": (step(1, INSTALL, output="installed"), step(2, CHECK, completed=False, output="boom")),
        "log": "installed\nboom",
    }
    defaults.update(overrides)
    return job(**defaults)


class CheckRunPublisherTest(unittest.TestCase):
    def publisher(self, responses=()) -> tuple[CheckRunPublisher, object]:
        client = GitHubClient("token")
        return CheckRunPublisher(client), client

    def test_a_queued_run_opens_its_check_run(self) -> None:
        publisher, _ = self.publisher()
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(b'{"check_runs": []}'), Response(b'{"id": 42}')]
            record = job(status=PENDING, device=None, attempts=0)
            published = publisher.publish(record)

        self.assertTrue(published.created)
        self.assertEqual(published.check_run_id, 42)
        listing, create = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual(listing.get_method(), "GET")
        self.assertIn(f"/commits/{SHA}/check-runs?check_name=cindral%2Fci", listing.full_url)
        self.assertEqual(create.get_method(), "POST")
        self.assertEqual(create.full_url, CHECKS)
        self.assertIn("Bearer token", create.get_header("Authorization"))
        body = json.loads(create.data)
        self.assertEqual(body["name"], DEFAULT_CHECK_NAME)
        self.assertEqual(body["head_sha"], SHA)
        self.assertEqual(body["external_id"], "job-1")
        self.assertEqual(body["status"], "queued")
        self.assertNotIn("conclusion", body)
        self.assertEqual(body["started_at"], iso8601(1_750_000_000.0))
        self.assertEqual(body["output"]["title"], "queued on the device pool")

    def test_a_run_a_device_holds_is_in_progress(self) -> None:
        publisher, _ = self.publisher()
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(b'{"check_runs": []}'), Response(b'{"id": 42}')]
            publisher.publish(job())

        body = json.loads(urlopen.call_args_list[1].args[0].data)
        self.assertEqual(body["status"], "in_progress")
        self.assertNotIn("completed_at", body)
        self.assertNotIn("annotations", body["output"])

    def test_a_step_completion_updates_the_check_the_run_already_opened(self) -> None:
        # the publisher updates the one check run it holds rather than opening a
        # second, which is what a caller that can see a record change needs. No
        # caller can yet: the agent-facing API has no way to hand the broker a
        # record mid-run, so this is the shape the path has to work in rather than
        # a step progress a reader of a commit sees today.
        publisher, _ = self.publisher()
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(b'{"check_runs": []}'), Response(b'{"id": 42}'), Response()]
            publisher.publish(job(steps=(step(1, INSTALL), step(2, CHECK, completed=False))))
            publisher.publish(job(steps=(step(1, INSTALL), step(2, CHECK))))

        self.assertEqual([call.args[0].get_method() for call in urlopen.call_args_list[1:]], ["POST", "PATCH"])
        update = urlopen.call_args_list[2].args[0]
        self.assertEqual(update.full_url, f"{CHECKS}/42")
        self.assertEqual(update.get_method(), "PATCH")
        body = json.loads(update.data)
        self.assertNotIn("head_sha", body)
        self.assertEqual(body["external_id"], "job-1")
        self.assertIn("| 2 | `" + CHECK + "` | passed |", body["output"]["summary"])

    def test_a_finished_run_carries_its_conclusion_log_and_annotation(self) -> None:
        publisher, _ = self.publisher()
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(b'{"check_runs": []}'), Response(b'{"id": 42}'), Response()]
            publisher.publish(job())
            publisher.publish(failing(), target_url="https://panel.example/api/jobs/job-1", now=1_750_000_100.0)

        body = json.loads(urlopen.call_args_list[2].args[0].data)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["conclusion"], "failure")
        self.assertEqual(body["completed_at"], iso8601(1_750_000_100.0))
        self.assertEqual(body["details_url"], "https://panel.example/api/jobs/job-1")
        self.assertEqual(body["output"]["title"], "device pool run failed (exit 1): step 2: " + CHECK)
        self.assertIn("boom", body["output"]["text"])
        (annotation,) = body["output"]["annotations"]
        self.assertEqual(annotation["title"], "cindral: step 2 failed")
        self.assertEqual(annotation["path"], CONTRACT_PATH)
        self.assertEqual(annotation["raw_details"], "boom")

    def test_a_passing_run_ends_the_check_green(self) -> None:
        publisher, _ = self.publisher()
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(b'{"check_runs": []}'), Response(b'{"id": 42}'), Response()]
            publisher.publish(job())
            publisher.publish(job(status=SUCCESS, exit_code=0, log="clean"))

        body = json.loads(urlopen.call_args_list[2].args[0].data)
        self.assertEqual(body["conclusion"], "success")
        self.assertNotIn("annotations", body["output"])
        # a green run's log is the one no reader opens, so it does not go on the
        # commit at all
        self.assertEqual(body["output"]["text"], "")

    def test_a_verdict_published_twice_annotates_once(self) -> None:
        # GitHub appends annotations on every update, and the broker republishes a
        # verdict whenever one it owed the commit reaches it late
        publisher, _ = self.publisher()
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [
                Response(b'{"check_runs": []}'),
                Response(b'{"id": 42}'),
                Response(),
                Response(),
            ]
            publisher.publish(job())
            publisher.publish(failing())
            publisher.publish(failing())

        first, second = (json.loads(call.args[0].data) for call in urlopen.call_args_list[2:])
        self.assertEqual(len(first["output"]["annotations"]), 1)
        self.assertNotIn("annotations", second["output"])
        self.assertEqual(second["conclusion"], "failure")

    def test_a_refused_update_leaves_the_run_open_so_the_retry_annotates(self) -> None:
        # the id and the finished flag are written only after a request returns,
        # so a verdict GitHub refused is still owed and is not mistaken for one
        # it already holds
        publisher = CheckRunPublisher(GitHubClient("token"))
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [
                Response(b'{"check_runs": []}'),
                Response(b'{"id": 42}'),
                refused(500, "server error"),
                Response(),
            ]
            publisher.publish(job())
            with self.assertRaises(GitHubAPIError):
                publisher.publish(failing())
            publisher.publish(failing())

        retried = json.loads(urlopen.call_args_list[3].args[0].data)
        self.assertEqual(len(retried["output"]["annotations"]), 1)
        self.assertEqual(retried["conclusion"], "failure")

    def test_a_lost_run_stays_open_until_its_attempts_run_out(self) -> None:
        publisher, _ = self.publisher()
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(b'{"check_runs": []}'), Response(b'{"id": 42}'), Response()]
            publisher.publish(job(status=PENDING, last_error="docker is not running"))
            publisher.publish(job(status=DEAD, attempts=MAX_ATTEMPTS, last_error="docker is not running"))

        waiting = json.loads(urlopen.call_args_list[1].args[0].data)
        parked = json.loads(urlopen.call_args_list[2].args[0].data)
        self.assertEqual(waiting["status"], "queued")
        self.assertNotIn("conclusion", waiting)
        self.assertEqual(parked["conclusion"], "failure")
        self.assertIn("did not pass", parked["output"]["annotations"][0]["title"])


class RestartTest(unittest.TestCase):
    def test_a_restarted_broker_updates_the_check_run_it_already_opened(self) -> None:
        # without the id, the report after a restart would open a second check for
        # the run and leave the first one pending on the commit forever
        found = json.dumps(
            {"check_runs": [{"id": 7, "status": "in_progress", "external_id": "job-1", "name": DEFAULT_CHECK_NAME}]}
        ).encode()
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(found), Response()]
            published = CheckRunPublisher(GitHubClient("token")).publish(failing())

        self.assertFalse(published.created)
        self.assertEqual(published.check_run_id, 7)
        listing, update = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual(listing.get_method(), "GET")
        self.assertEqual(update.full_url, f"{CHECKS}/7")
        self.assertEqual(update.get_method(), "PATCH")
        self.assertEqual(json.loads(update.data)["conclusion"], "failure")

    def test_a_restarted_broker_does_not_annotate_a_check_it_already_finished(self) -> None:
        found = json.dumps(
            {"check_runs": [{"id": 7, "status": "completed", "external_id": "job-1", "name": DEFAULT_CHECK_NAME}]}
        ).encode()
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(found), Response()]
            CheckRunPublisher(GitHubClient("token")).publish(failing())

        body = json.loads(urlopen.call_args_list[1].args[0].data)
        self.assertNotIn("annotations", body["output"])
        self.assertEqual(body["conclusion"], "failure")

    def test_another_runs_check_on_the_same_commit_is_left_alone(self) -> None:
        found = json.dumps(
            {"check_runs": [{"id": 3, "status": "completed", "external_id": "job-9", "name": DEFAULT_CHECK_NAME}]}
        ).encode()
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(found), Response(b'{"id": 42}')]
            published = CheckRunPublisher(GitHubClient("token")).publish(job())

        self.assertTrue(published.created)
        self.assertEqual(urlopen.call_args_list[1].args[0].get_method(), "POST")

    def test_an_unreadable_check_run_list_is_an_error(self) -> None:
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(b'{"check_runs": {}}')]
            with self.assertRaises(GitHubAPIError):
                CheckRunPublisher(GitHubClient("token")).publish(job())


class DeletedCheckRunTest(unittest.TestCase):
    """A check run GitHub no longer has must not strand every later publish."""

    def test_a_deleted_check_run_is_reopened_rather_than_updated_forever(self) -> None:
        publisher = CheckRunPublisher(GitHubClient("token"))
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [
                Response(b'{"check_runs": []}'),
                Response(b'{"id": 42}'),
                refused(404, "Not Found"),
                Response(b'{"check_runs": []}'),
                Response(b'{"id": 43}'),
            ]
            publisher.publish(job())
            published = publisher.publish(failing())

        self.assertTrue(published.created)
        self.assertEqual(published.check_run_id, 43)
        self.assertEqual(
            [call.args[0].get_method() for call in urlopen.call_args_list],
            ["GET", "POST", "PATCH", "GET", "POST"],
        )
        self.assertEqual(json.loads(urlopen.call_args_list[4].args[0].data)["conclusion"], "failure")

    def test_a_deleted_check_run_that_github_still_lists_is_updated(self) -> None:
        # a check run can be replaced rather than removed, and the run's own
        # external id is what says the replacement is the same check
        found = json.dumps(
            {"check_runs": [{"id": 9, "status": "in_progress", "external_id": "job-1", "name": DEFAULT_CHECK_NAME}]}
        ).encode()
        publisher = CheckRunPublisher(GitHubClient("token"))
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [
                Response(b'{"check_runs": []}'),
                Response(b'{"id": 42}'),
                refused(404, "Not Found"),
                Response(found),
                Response(),
            ]
            publisher.publish(job())
            published = publisher.publish(failing())

        self.assertFalse(published.created)
        self.assertEqual(published.check_run_id, 9)
        self.assertEqual(urlopen.call_args_list[4].args[0].full_url, f"{CHECKS}/9")

    def test_a_repository_github_will_not_serve_is_raised_rather_than_retried(self) -> None:
        publisher = CheckRunPublisher(GitHubClient("token"))
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [
                Response(b'{"check_runs": []}'),
                Response(b'{"id": 42}'),
                refused(404, "Not Found"),
                Response(b'{"check_runs": []}'),
                refused(404, "Not Found"),
            ]
            publisher.publish(job())
            with self.assertRaises(CheckRunAPIError):
                publisher.publish(failing())

        self.assertEqual(
            [call.args[0].get_method() for call in urlopen.call_args_list],
            ["GET", "POST", "PATCH", "GET", "POST"],
        )

    def test_a_refusal_that_is_not_a_missing_check_is_raised_at_once(self) -> None:
        publisher = CheckRunPublisher(GitHubClient("token"))
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(b'{"check_runs": []}'), Response(b'{"id": 42}'), refused(403, "forbidden")]
            publisher.publish(job())
            with self.assertRaises(CheckRunAPIError) as raised:
                publisher.publish(failing())

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual([call.args[0].get_method() for call in urlopen.call_args_list], ["GET", "POST", "PATCH"])


class ThrottleTest(unittest.TestCase):
    """A throttled App and a missing permission are not the same answer."""

    def refused_error(self, code: int, headers=None) -> CheckRunAPIError:
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(b'{"check_runs": []}'), refused(code, "throttled", headers)]
            with self.assertRaises(GitHubAPIError) as raised:
                publish_check_run(GitHubClient("token"), job())
        return raised.exception

    def test_a_refusal_carries_the_code_github_answered_with(self) -> None:
        for code in (403, 422, 429, 500):
            with self.subTest(code=code):
                self.assertEqual(self.refused_error(code).status_code, code)

    def test_a_commit_the_credential_cannot_read_is_not_recovered_from(self) -> None:
        # a 404 on the lookup is a commit GitHub will not serve, and looking it
        # up again is the same answer twice
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [refused(404, "Not Found")]
            with self.assertRaises(CheckRunAPIError) as raised:
                publish_check_run(GitHubClient("token"), job())

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(len(urlopen.call_args_list), 1)

    def test_a_secondary_rate_limit_is_retryable_with_the_wait_github_named(self) -> None:
        # GitHub answers a throttled App with 403 as readily as 429, and the
        # header is the only thing that separates the throttle from a permission
        # this App will never be granted
        for code in (403, 429):
            with self.subTest(code=code):
                error = self.refused_error(code, {"Retry-After": "42"})
                self.assertEqual(error.retry_after, 42)
                self.assertTrue(error.retryable)

    def test_a_missing_permission_is_not_something_to_wait_for(self) -> None:
        error = self.refused_error(403, {"X-GitHub-Media-Type": "github.v3"})
        self.assertIsNone(error.retry_after)
        self.assertFalse(error.retryable)

    def test_a_wait_this_module_cannot_read_is_still_a_wait(self) -> None:
        # an unreadable header is a throttle, and reading it as a permission is
        # how a run's check run is dropped for a request that would have worked
        error = self.refused_error(429, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
        self.assertEqual(error.retry_after, 60.0)
        self.assertTrue(error.retryable)

    def test_a_request_that_never_reached_github_is_retryable(self) -> None:
        from urllib.error import URLError

        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(b'{"check_runs": []}'), URLError("connection reset")]
            with self.assertRaises(GitHubAPIError) as raised:
                publish_check_run(GitHubClient("token"), job())

        self.assertIsNone(raised.exception.status_code)
        self.assertTrue(raised.exception.retryable)


class NameKeyTest(unittest.TestCase):
    def test_a_renamed_status_context_opens_its_own_check(self) -> None:
        # the id is the only record of which check a job owns, and a renamed
        # context is a differently named check: reusing the id would leave the
        # check under the old name pending on the commit forever, which is the
        # outcome the id exists to prevent
        publisher = CheckRunPublisher(GitHubClient("token"))
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [
                Response(b'{"check_runs": []}'),
                Response(b'{"id": 42}'),
                Response(b'{"check_runs": []}'),
                Response(b'{"id": 43}'),
            ]
            publisher.publish(job())
            published = publisher.publish(job(), name="cindral/device")

        self.assertTrue(published.created)
        self.assertEqual(published.check_run_id, 43)
        self.assertEqual(json.loads(urlopen.call_args_list[3].args[0].data)["name"], "cindral/device")
        self.assertIn("check_name=cindral%2Fdevice", urlopen.call_args_list[2].args[0].full_url)


class PagingTest(unittest.TestCase):
    """A run's own check can sit past the first page of a repeatedly retried commit."""

    def page(self, entries: int) -> bytes:
        entry = {"id": 3, "status": "completed", "external_id": "job-9", "name": DEFAULT_CHECK_NAME}
        return json.dumps({"check_runs": [entry] * entries}).encode()

    def test_a_check_run_past_the_first_page_is_still_found(self) -> None:
        found = json.dumps(
            {"check_runs": [{"id": 7, "status": "in_progress", "external_id": "job-1", "name": DEFAULT_CHECK_NAME}]}
        ).encode()
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(self.page(100)), Response(found), Response()]
            published = CheckRunPublisher(GitHubClient("token")).publish(job())

        self.assertFalse(published.created)
        self.assertEqual(published.check_run_id, 7)
        self.assertIn("per_page=100&page=1", urlopen.call_args_list[0].args[0].full_url)
        self.assertIn("per_page=100&page=2", urlopen.call_args_list[1].args[0].full_url)

    def test_more_checks_under_one_name_than_the_cap_refuses_to_guess(self) -> None:
        # a prefix that stopped short of the run's own check would open a second
        # one, which is the outcome the recovery exists to prevent
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(self.page(100))] * 5
            with self.assertRaises(GitHubAPIError):
                CheckRunPublisher(GitHubClient("token")).publish(job())

        self.assertEqual(len(urlopen.call_args_list), 5)
        self.assertNotIn("POST", [call.args[0].get_method() for call in urlopen.call_args_list])


class PublishedPayloadTest(unittest.TestCase):
    """What the request body carries is what the commit shows."""

    def publish_body(self, record: Job) -> dict:
        publisher = CheckRunPublisher(GitHubClient("token"))
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(b'{"check_runs": []}'), Response(b'{"id": 42}')]
            publisher.publish(record)
        return json.loads(urlopen.call_args_list[1].args[0].data)

    def test_a_failing_run_publishes_no_credential_anywhere_in_the_body(self) -> None:
        steps = (step(1, INSTALL, output="installed"), step(2, CHECK, completed=False, output="boom " + SECRET))
        body = self.publish_body(
            failing(steps=steps, log="installed\nboom " + SECRET + "\n", last_error="x " + SECRET)
        )
        self.assertNotIn(SECRET, json.dumps(body))
        self.assertIn("boom", body["output"]["text"])
        self.assertEqual(len(body["output"]["annotations"]), 1)

    def test_a_passing_run_publishes_no_log_and_no_annotation(self) -> None:
        body = self.publish_body(job(status=SUCCESS, exit_code=0, log="all clean " + SECRET))
        self.assertEqual(body["output"]["text"], "")
        self.assertNotIn("annotations", body["output"])
        self.assertNotIn(SECRET, json.dumps(body))

    def test_every_output_field_is_inside_the_size_github_accepts(self) -> None:
        steps = tuple(step(index, CHECK, output="x" * 16_000) for index in range(1, 65))
        body = self.publish_body(failing(steps=steps, log="y" * 65_536))
        self.assertLessEqual(len(body["output"]["summary"]), 65535)
        self.assertLessEqual(len(body["output"]["text"]), 65535)
        self.assertLessEqual(len(body["output"]["annotations"][0]["raw_details"]), 64 * 1024)


class EntryPointTest(unittest.TestCase):
    def test_one_client_keeps_one_publisher_and_asks_github_once(self) -> None:
        client = GitHubClient("token")
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [
                Response(b'{"check_runs": []}'),
                Response(b'{"id": 42}'),
                Response(),
                Response(),
            ]
            publish_check_run(client, job())
            publish_check_run(client, failing())
            publish_check_run(client, failing())

        methods = [call.args[0].get_method() for call in urlopen.call_args_list]
        self.assertEqual(methods, ["GET", "POST", "PATCH", "PATCH"])

    def test_a_credential_provider_is_resolved_for_every_check_run_request(self) -> None:
        calls: list[int] = []

        def provider() -> str:
            calls.append(1)
            return f"installation-token-{len(calls)}"

        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(b'{"check_runs": []}'), Response(b'{"id": 42}')]
            publish_check_run(GitHubClient(provider), job())

        sent = [call.args[0].get_header("Authorization") for call in urlopen.call_args_list]
        self.assertEqual(sent, ["Bearer installation-token-1", "Bearer installation-token-2"])

    def test_a_refused_check_run_is_raised_for_the_caller_to_absorb(self) -> None:
        # an App without checks: write refuses the create, and the caller keeps
        # posting the commit status it already posts
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(b'{"check_runs": []}'), refused(403, "forbidden")]
            with self.assertRaises(GitHubAPIError):
                publish_check_run(GitHubClient("token"), job())

    def test_a_run_with_no_usable_commit_is_refused(self) -> None:
        for record in (job(repository="../repo"), job(sha="", status_sha="")):
            with self.subTest(repository=record.repository, sha=record.sha), self.assertRaises(ValueError):
                CheckRunPublisher(GitHubClient("token")).publish(record)

    def test_a_check_run_created_without_an_id_is_an_error(self) -> None:
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(b'{"check_runs": []}'), Response(b'{"id": "42"}')]
            with self.assertRaises(GitHubAPIError):
                CheckRunPublisher(GitHubClient("token")).publish(job())

    def test_the_pull_request_head_is_the_commit_the_status_names(self) -> None:
        with patch("cindral.check_runs.urlopen") as urlopen:
            urlopen.side_effect = [Response(b'{"check_runs": []}'), Response(b'{"id": 42}')]
            CheckRunPublisher(GitHubClient("token")).publish(job(status_sha="b" * 40))

        listing, create = urlopen.call_args_list[0].args[0], urlopen.call_args_list[1].args[0]
        self.assertIn(f"/commits/{'b' * 40}/check-runs", listing.full_url)
        self.assertEqual(json.loads(create.data)["head_sha"], "b" * 40)


class ProducerTest(unittest.TestCase):
    """Where the check run is published from, asserted rather than claimed in a document."""

    #: the three points a run's state changes. This test was originally the
    #: inverse, asserting that no caller existed, and it failed on purpose the
    #: moment the first one landed: an unwired producer is a feature that
    #: publishes nothing, so the wiring is the change that makes it real.
    CALL_SITES = {
        "service.py": "where a run is queued",
        "jobapi.py": "where a device reports a verdict and where an owed status drains",
        # the reclaim timer settles most lapsed leases now, so a check opened at
        # enqueue would stay in progress on every commit it settles
        "lease_reclaim.py": "where the reclaim timer publishes a verdict it owed",
    }

    def publishers(self) -> dict[str, str]:
        return {
            path.name: path.read_text(encoding="utf-8")
            for path in Path(cindral.__file__).parent.glob("*.py")
            if path.name != "check_runs.py" and "publish_check_run_quietly" in path.read_text(encoding="utf-8")
        }

    def test_every_expected_call_site_publishes_a_check_run(self) -> None:
        # named rather than counted, so a fourth caller is a visible addition
        # instead of a number that drifts
        self.assertEqual(sorted(self.publishers()), sorted(self.CALL_SITES))

    def test_the_producer_is_not_called_bypassing_the_absorbing_wrapper(self) -> None:
        # a raw publish_check_run at a call site re-decides the refusal policy
        # that publish_check_run_quietly exists to hold in one place
        for name, text in self.publishers().items():
            with self.subTest(module=name):
                self.assertNotIn("publish_check_run(", text)

    def test_the_queue_publishes_the_check_before_the_commit_status(self) -> None:
        # opening the check is what makes a queued run visible on the commit; if
        # the status were posted first and this raised, the run would be enqueued
        # with no check at all until something else published one
        #
        # The second index is anchored past the import, or the first one matches
        # the import line and the assertion passes without reaching the call site.
        source = self.publishers()["service.py"]
        enqueued = source.index("jobs.enqueue(")
        self.assertLess(
            source.index("publish_check_run_quietly", enqueued),
            source.index("post_status", enqueued),
        )

    def test_a_report_closes_the_check_from_the_recorded_verdict(self) -> None:
        # store.report returns the job carrying the verdict; publishing from the
        # pre-record job would close the check while it still reads in progress
        source = self.publishers()["jobapi.py"]
        report = source.index("job = store.report(")
        self.assertLess(report, source.index("publish_check_run_quietly", report))

    def test_no_device_endpoint_can_hand_over_a_mid_run_record(self) -> None:
        # the update path exists for a progress endpoint, and there is none: the
        # agent-facing API mutates a run through claim, renew, and report, and a
        # report without an exit code is refused, so a device can only speak once
        # the run has ended. The check opens on enqueue and closes on the verdict
        from cindral.jobapi import JobApiMixin

        actions = {
            constant
            for constant in JobApiMixin._jobs.__code__.co_consts
            if isinstance(constant, (set, frozenset))
        }
        self.assertIn(frozenset({"renew", "report"}), actions)
        self.assertIn("/v1/jobs/claim", JobApiMixin._jobs.__code__.co_consts)
        for absent in ("/v1/jobs/progress", "/v1/jobs/step", "/v1/jobs/update"):
            with self.subTest(endpoint=absent):
                self.assertNotIn(absent, JobApiMixin._jobs.__code__.co_consts)
        self.assertIn("a report needs an exit code or a device error", JobApiMixin._report.__code__.co_consts)


if __name__ == "__main__":
    unittest.main()

"""What a device run renders as a check run: the step table, the log, the annotation."""
import unittest
from dataclasses import replace

from cindral.check_summary import (
    DEFAULT_CHECK_NAME,
    MAX_OUTPUT_CHARS,
    PASSED_DESCRIPTION,
    QUEUED_DESCRIPTION,
    TIMED_OUT_EXIT,
    annotations_for,
    conclusion_for,
    log_text,
    state_for,
    summary_for,
    title_for,
    verdict_for,
)
from cindral.contract import CONTRACT_PATH
from cindral.github import GitHubClient
from cindral.jobrecord import Job
from cindral.jobs import FAILURE, SUCCESS
from cindral.lease_reclaim import DEAD, PENDING, RUNNING
from cindral.retry import LEASE_LOST, MAX_ATTEMPTS
from cindral.status_text import failure_description, lost_description

SHA = "a" * 40
INSTALL = "corepack enable && pnpm install --frozen-lockfile"
LINT = "pnpm lint"
CHECK = "uv run python scripts/check.py"


def step(index: int, name: str, *, completed: bool = True, output: str = "", **extra) -> dict:
    return {
        "index": index,
        "name": name,
        "output": output,
        "completed": completed,
        "truncated": False,
        "dropped": 0,
        **extra,
    }


STEPS = (
    step(1, INSTALL, output="installed"),
    step(2, LINT, output="lint clean"),
    step(3, CHECK, completed=False, output="FAIL tests.test_github"),
)
LOG = "installed\nlint clean\nFAIL tests.test_github"


def job(**overrides) -> Job:
    base = Job(
        id="job-1",
        repository="example-org/example-app",
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


class TreeNamingTest(unittest.TestCase):
    """A run must name the tree it built, not only the ref it was given.

    A merge ref is recomputed on every push to the branch, so a log naming the ref
    gives a reader a target to go and resolve rather than an answer, and resolving
    it later can give a different commit than the run got. This is the reader's
    only route to deciding whether a verdict describes the tree they are looking
    at, so the sha has to be in it.
    """

    def test_a_merge_run_names_the_tree_it_built_and_the_head_it_contains(self) -> None:
        summary = summary_for(
            job(ref="refs/pull/212/merge", sha="merge123", status_sha="head123", status=SUCCESS)
        )
        self.assertIn("built `merge123`", summary)
        self.assertIn("head `head123`", summary)

    def test_a_push_run_does_not_repeat_its_head_as_a_separate_value(self) -> None:
        """A push builds the commit the event named, so naming it twice is noise.

        The head only earns its place where it differs, which is a merge: the
        whole question there is whether the built tree contained the branch head.
        """
        summary = summary_for(
            job(ref="refs/heads/main", sha=SHA, status_sha=SHA, status=SUCCESS)
        )
        self.assertIn(f"built `{SHA}`", summary)
        self.assertNotIn("head `", summary)

    def test_the_built_tree_is_named_even_when_no_head_was_recorded(self) -> None:
        summary = summary_for(job(ref="refs/heads/main", sha=SHA, status=SUCCESS))
        self.assertIn(f"`refs/heads/main`, built `{SHA}`", summary)


def finished(**overrides) -> Job:
    defaults = {"steps": STEPS, "log": LOG}
    defaults.update(overrides)
    return job(**defaults)


def passed(**overrides) -> Job:
    defaults = {"steps": tuple({**step, "completed": True} for step in STEPS), "log": "all clean"}
    defaults.update(overrides)
    return job(status=SUCCESS, exit_code=0, **defaults)


def _sent_literals(function) -> set[str]:
    """The strings one handler sends, including those folded into a tuple constant."""
    sent: set[str] = set()
    for constant in function.__code__.co_consts:
        if isinstance(constant, str):
            sent.add(constant)
        elif isinstance(constant, tuple):
            sent.update(item for item in constant if isinstance(item, str))
    return sent


class StepTableTest(unittest.TestCase):
    def test_every_step_is_listed_with_its_result(self) -> None:
        summary = summary_for(finished(status=FAILURE, exit_code=1))
        self.assertIn("| 1 | `" + INSTALL + "` | passed |", summary)
        self.assertIn("| 2 | `" + LINT + "` | passed |", summary)
        self.assertIn("| 3 | `" + CHECK + "` | **failed** |", summary)

    def test_the_step_named_is_the_one_the_commit_status_names(self) -> None:
        # steps.unfinished_step reads the last step that did not complete, so a
        # record holding two of them has to name the same one here or the status
        # and the check would point at different steps
        record = finished(
            status=FAILURE,
            exit_code=2,
            steps=(step(1, INSTALL), step(2, LINT, completed=False), step(3, CHECK, completed=False)),
        )
        self.assertIn("step 3: " + CHECK, title_for(record))
        self.assertIn("| 2 | `" + LINT + "` | not finished |", summary_for(record))
        self.assertIn("Step 3 failed", summary_for(record))

    def test_a_passing_run_lists_every_step_as_passed(self) -> None:
        verdict = verdict_for(passed())
        self.assertEqual(verdict.conclusion, "success")
        self.assertNotIn("failed", verdict.summary)
        self.assertEqual(verdict.summary.count("| passed |"), 3)

    def test_the_step_in_progress_is_marked_while_the_run_goes(self) -> None:
        verdict = verdict_for(finished())
        self.assertIsNone(verdict.conclusion)
        self.assertIn("Step 3 is running", verdict.summary)
        self.assertIn("| 3 | `" + CHECK + "` | running |", verdict.summary)

    def test_a_step_command_cannot_break_the_table(self) -> None:
        # a step's name is repository-controlled, so a pipe, a backtick, or a
        # newline in it would break the table or the code span around it
        summary = summary_for(
            finished(
                status=SUCCESS,
                exit_code=0,
                steps=(step(1, "echo a | b"), step(2, "echo `tick`", completed=True), step(3, "echo one\ntwo")),
            )
        )
        rows = [line for line in summary.splitlines() if line.startswith("| ")]
        self.assertEqual(len(rows), 5)
        self.assertIn("| 1 | `echo a \\| b` | passed |", rows)
        self.assertIn("| 2 | `echo 'tick'` | passed |", rows)
        self.assertIn("| 3 | `echo one two` | passed |", rows)

    def test_a_step_the_record_dropped_is_disclosed(self) -> None:
        summary = summary_for(
            finished(status=FAILURE, exit_code=1, steps=(step(1, INSTALL), step(2, CHECK, completed=False, dropped=3)))
        )
        self.assertIn("3 earlier steps were not kept in this record.", summary)

    def test_a_truncated_step_output_is_disclosed(self) -> None:
        summary = summary_for(
            finished(status=FAILURE, exit_code=1, steps=(step(1, CHECK, completed=False, truncated=True),))
        )
        self.assertIn("Some step output was cut", summary)

    def test_a_summary_over_the_limit_is_cut_and_says_so(self) -> None:
        # GitHub rejects a request whose output field is too long, so a summary
        # past the limit would publish no check at all. The store caps a step
        # name well below this; the bound is what keeps a record that did not come
        # through the store from taking the check run down with it.
        steps = tuple(step(index, "x" * 2000) for index in range(1, 65))
        summary = summary_for(job(steps=steps, status=SUCCESS, exit_code=0))
        self.assertLessEqual(len(summary), MAX_OUTPUT_CHARS)
        self.assertIn("cut to fit GitHub's size limit", summary)


class VerdictTest(unittest.TestCase):
    def test_a_failing_step_is_never_reported_as_passing(self) -> None:
        verdict = verdict_for(finished(status=FAILURE, exit_code=1))
        self.assertEqual(verdict.conclusion, "failure")
        self.assertIn("Step 3 failed", verdict.summary)
        self.assertNotIn("| 3 | `" + CHECK + "` | passed |", verdict.summary)

    def test_a_run_with_no_verdict_has_no_conclusion(self) -> None:
        for record in (job(), job(status=PENDING, device=None, attempts=0)):
            with self.subTest(record=record.status):
                self.assertIsNone(verdict_for(record).conclusion)
                self.assertEqual(verdict_for(record).annotations, ())

    def test_a_run_that_died_before_the_first_step_says_so(self) -> None:
        verdict = verdict_for(
            job(status=FAILURE, exit_code=1, log="cindral: dockerfile not found: ci/Dockerfile\n")
        )
        self.assertIn("failed without a step record", verdict.summary)
        self.assertIn("No step ran.", verdict.summary)

    def test_a_lost_run_is_pending_until_its_attempts_run_out(self) -> None:
        lost = job(status=PENDING, last_error="docker is not running")
        self.assertEqual(state_for(lost), "pending")
        self.assertIsNone(verdict_for(lost).conclusion)
        self.assertEqual(verdict_for(lost).annotations, ())

        parked = replace(lost, status=DEAD, attempts=MAX_ATTEMPTS)
        self.assertEqual(state_for(parked), "error")
        self.assertEqual(verdict_for(parked).conclusion, "failure")
        self.assertEqual(len(verdict_for(parked).annotations), 1)
        self.assertIn("will not be retried", verdict_for(parked).summary)

    def test_a_lost_run_says_it_is_waiting_to_be_claimed_again(self) -> None:
        summary = summary_for(job(status=PENDING, last_error="the device lease expired before the run finished"))
        self.assertIn("The run did not finish", summary)
        self.assertIn(f"Waiting for attempt 2 of {MAX_ATTEMPTS}.", summary)
        self.assertNotIn("has not started yet", summary)

    def test_a_queued_run_says_it_is_waiting_for_a_device(self) -> None:
        summary = summary_for(job(status=PENDING, device=None, attempts=0))
        self.assertIn("Waiting for a device.", summary)
        self.assertIn("not claimed yet", summary)


class AnnotationTest(unittest.TestCase):
    def test_the_failing_step_is_named_with_its_own_output(self) -> None:
        (annotation,) = annotations_for(finished(status=FAILURE, exit_code=1))
        self.assertEqual(annotation["path"], CONTRACT_PATH)
        self.assertEqual(annotation["annotation_level"], "failure")
        self.assertEqual(annotation["title"], "cindral: step 3 failed")
        self.assertEqual(annotation["raw_details"], "FAIL tests.test_github")
        self.assertEqual((annotation["start_line"], annotation["end_line"]), (1, 1))

    def test_the_annotation_carries_the_sentence_the_commit_status_carries(self) -> None:
        record = finished(status=FAILURE, exit_code=1)
        (annotation,) = annotations_for(record)
        self.assertEqual(annotation["message"], failure_description(1, record.log, record.steps))

    def test_a_parked_run_annotates_the_loss_rather_than_a_step(self) -> None:
        (annotation,) = annotations_for(
            job(status=DEAD, attempts=MAX_ATTEMPTS, last_error="docker is not running")
        )
        self.assertIn("did not pass", annotation["title"])
        self.assertEqual(annotation["raw_details"], "docker is not running")

    def test_the_annotation_path_is_the_operators_to_choose(self) -> None:
        (annotation,) = annotations_for(finished(status=FAILURE, exit_code=1), path=".github/workflows/ci.yml")
        self.assertEqual(annotation["path"], ".github/workflows/ci.yml")

    def test_the_failing_step_output_is_bounded(self) -> None:
        (annotation,) = annotations_for(
            finished(status=FAILURE, exit_code=1, steps=(step(1, CHECK, completed=False, output="x" * 100_000),))
        )
        self.assertLessEqual(len(annotation["raw_details"]), 64 * 1024)


class LogTextTest(unittest.TestCase):
    def test_the_log_tail_is_fenced(self) -> None:
        text = log_text(finished(status=FAILURE, exit_code=1))
        self.assertTrue(text.startswith("```\n"))
        self.assertTrue(text.endswith("\n```"))
        self.assertIn("FAIL tests.test_github", text)

    def test_a_log_printing_its_own_fence_cannot_close_the_block(self) -> None:
        text = log_text(job(log="```\nnot the end of the log\n", status=FAILURE, exit_code=1))
        self.assertTrue(text.startswith("````\n"))
        self.assertTrue(text.endswith("\n````"))

    def test_a_log_one_character_over_the_limit_is_still_accepted(self) -> None:
        # the store keeps 64 KiB and GitHub accepts one character less, so a run
        # that filled the log arrives too long. The tail is what the bound exists
        # to keep: cutting it instead leaves a reader of a red commit looking at
        # the install log with the failure cut off the end, which is the one
        # thing they opened the check to see.
        text = log_text(job(log="x" * 65_536, status=FAILURE, exit_code=1))
        self.assertLessEqual(len(text), MAX_OUTPUT_CHARS)
        self.assertIn("the beginning of this log is not shown", text)
        self.assertIn("x" * 100, text)

    def test_a_log_far_over_the_limit_keeps_its_tail(self) -> None:
        # a run whose log is many times the limit is the normal case for a test
        # suite, and the slice has to come off the front for every size, not only
        # for one just over it
        text = log_text(job(log="a" * 200_000 + "FAIL the last test", status=FAILURE, exit_code=1))
        self.assertLessEqual(len(text), MAX_OUTPUT_CHARS)
        self.assertIn("FAIL the last test", text)
        self.assertNotIn("FAIL the last test" * 2, text)

    def test_a_short_log_is_left_whole(self) -> None:
        text = log_text(job(log="hello", status=FAILURE, exit_code=1))
        self.assertEqual(text, "```\nhello\n```")

    def test_a_run_with_no_log_renders_no_text(self) -> None:
        self.assertEqual(log_text(job(status=FAILURE, exit_code=1)), "")


class CredentialTest(unittest.TestCase):
    """A commit is public, so nothing a device printed reaches it as it was."""

    SECRET = "ghp_" + "b" * 36

    def fields(self, record: Job) -> dict[str, str]:
        verdict = verdict_for(record)
        annotations = " ".join(str(value) for value in verdict.annotations)
        return {
            "title": verdict.title,
            "summary": verdict.summary,
            "text": verdict.text,
            "annotations": annotations,
        }

    def assertNoSecret(self, record: Job) -> None:
        for field, rendered in self.fields(record).items():
            with self.subTest(field=field):
                self.assertNotIn(self.SECRET, rendered)

    def test_a_passing_run_publishes_no_log_at_all(self) -> None:
        # a green run's log is the one no reader opens, and a passing run is the
        # one that must not put a device's output on a public commit
        verdict = verdict_for(job(status=SUCCESS, exit_code=0, log="all clean\n" + self.SECRET))
        self.assertEqual(verdict.conclusion, "success")
        self.assertEqual(verdict.text, "")
        self.assertNoSecret(job(status=SUCCESS, exit_code=0, log="all clean\n" + self.SECRET))

    def test_a_failing_runs_log_is_scrubbed(self) -> None:
        steps = (step(1, CHECK, completed=False, output="boom " + self.SECRET),)
        record = finished(status=FAILURE, exit_code=1, steps=steps, log="boom " + self.SECRET)
        verdict = verdict_for(record)
        self.assertIn("[redacted]", verdict.text)
        self.assertIn("boom", verdict.text)
        self.assertIn("[redacted]", verdict.annotations[0]["raw_details"])
        self.assertNoSecret(record)

    def test_a_scrubbed_log_says_so(self) -> None:
        # a reader who finds a marker in the middle of a log should be able to
        # tell a removed credential from a tool that printed the word
        text = log_text(job(log="export TOKEN=" + self.SECRET, status=FAILURE, exit_code=1))
        self.assertIn("credentials in this log were removed", text)
        self.assertIn("[redacted]", text)

    def test_a_log_with_nothing_to_remove_is_left_alone(self) -> None:
        text = log_text(job(log="installed 12 packages\nlint clean", status=FAILURE, exit_code=1))
        self.assertEqual(text, "```\ninstalled 12 packages\nlint clean\n```")
        self.assertNotIn("credentials in this log were removed", text)

    def test_the_helpers_scrub_a_record_that_reached_them_unscrubbed(self) -> None:
        # verdict_for scrubs the record once, so these two are the paths a caller
        # reaching past it would take, and they have to hold the line themselves
        steps = (step(1, CHECK, completed=False, output="boom " + self.SECRET),)
        record = finished(status=FAILURE, exit_code=1, steps=steps, log="boom " + self.SECRET)
        self.assertNotIn(self.SECRET, log_text(record))
        (annotation,) = annotations_for(record)
        self.assertNotIn(self.SECRET, annotation["raw_details"])
        self.assertNotIn(self.SECRET, annotation["message"])

    def test_the_loss_a_device_reported_is_scrubbed(self) -> None:
        # a device error is a raised exception, and a raised exception carrying a
        # URL with a password in it is an ordinary way to fail
        record = job(
            status=DEAD,
            attempts=MAX_ATTEMPTS,
            last_error="RuntimeError: could not reach https://user:hunter2@registry.example/v2",
        )
        verdict = verdict_for(record)
        self.assertIn("registry.example/v2", verdict.summary)
        self.assertNotIn("hunter2", verdict.summary)
        self.assertIn("[redacted]", verdict.summary)
        self.assertNoSecret(record)


class ConclusionTest(unittest.TestCase):
    """The conclusion is read off the loss, which a commit status cannot say."""

    def test_a_run_that_hit_its_own_limit_is_not_a_failure(self) -> None:
        record = finished(status=FAILURE, exit_code=TIMED_OUT_EXIT)
        self.assertEqual(state_for(record), "failure")
        self.assertEqual(verdict_for(record).conclusion, "timed_out")
        self.assertIn("Step 3 did not finish", verdict_for(record).summary)
        self.assertIn("**did not finish**", verdict_for(record).summary)
        self.assertEqual(verdict_for(record).annotations[0]["title"], "cindral: step 3 did not finish")

    def test_a_run_taken_from_its_device_is_cancelled(self) -> None:
        # the loss is the lease going away, which is not a change that broke and
        # is not a limit the run reached: the device stopped answering
        record = job(status=DEAD, attempts=MAX_ATTEMPTS, last_error=LEASE_LOST)
        self.assertEqual(state_for(record), "error")
        self.assertEqual(verdict_for(record).conclusion, "cancelled")
        self.assertEqual(len(verdict_for(record).annotations), 1)

    def test_a_device_that_could_not_run_the_work_stays_red(self) -> None:
        record = job(status=DEAD, attempts=MAX_ATTEMPTS, last_error="docker is not running")
        self.assertEqual(verdict_for(record).conclusion, "failure")

    def test_every_conclusion_is_one_github_accepts(self) -> None:
        shapes = (
            job(status=SUCCESS, exit_code=0),
            finished(status=FAILURE, exit_code=1),
            finished(status=FAILURE, exit_code=TIMED_OUT_EXIT),
            job(status=DEAD, attempts=MAX_ATTEMPTS, last_error=LEASE_LOST),
            job(status=DEAD, attempts=MAX_ATTEMPTS, last_error="docker is not running"),
            job(status=PENDING, last_error=LEASE_LOST),
            job(),
        )
        accepted = {
            "action_required", "cancelled", "failure", "neutral",
            "skipped", "stale", "success", "timed_out",
        }
        for record in shapes:
            with self.subTest(status=record.status, exit_code=record.exit_code):
                conclusion = conclusion_for(record)
                self.assertTrue(conclusion is None or conclusion in accepted)
                # the two surfaces can only differ in which red they name, never
                # in whether the run passed
                self.assertEqual(conclusion == "success", state_for(record) == "success")

    def test_a_green_conclusion_is_never_produced_for_a_run_that_did_not_pass(self) -> None:
        for record in (finished(status=FAILURE, exit_code=1), job(), job(status=PENDING, last_error=LEASE_LOST)):
            with self.subTest(status=record.status):
                self.assertNotEqual(conclusion_for(record), "success")


class CommitStatusAgreementTest(unittest.TestCase):
    """The check run and the commit status are two renderings of one verdict."""

    def test_the_conclusion_never_contradicts_the_status_state(self) -> None:
        shapes = {
            "passed": job(status=SUCCESS, exit_code=0),
            "failed": finished(status=FAILURE, exit_code=1),
            "queued": job(status=PENDING, device=None, attempts=0),
            "running": job(),
            "retrying": job(status=PENDING, last_error="docker is not running"),
            "parked": job(status=DEAD, attempts=MAX_ATTEMPTS, last_error="docker is not running"),
        }
        for label, record in shapes.items():
            with self.subTest(shape=label):
                verdict = verdict_for(record)
                if verdict.state in {"success", "failure", "error"}:
                    self.assertIsNotNone(verdict.conclusion)
                else:
                    self.assertIsNone(verdict.conclusion)
                self.assertEqual(verdict.state in {"success"}, verdict.conclusion == "success")

    def test_a_failing_step_cannot_report_the_commit_as_green(self) -> None:
        for exit_code in (1, 2, 127):
            with self.subTest(exit_code=exit_code):
                record = finished(status=FAILURE, exit_code=exit_code)
                self.assertEqual(state_for(record), "failure")
                self.assertEqual(verdict_for(record).conclusion, "failure")

    def test_the_check_title_is_the_sentence_the_status_path_sends(self) -> None:
        record = finished(status=FAILURE, exit_code=1)
        self.assertEqual(title_for(record), failure_description(1, record.log, record.steps))
        lost = job(status=DEAD, attempts=MAX_ATTEMPTS, last_error="docker is not running")
        self.assertEqual(title_for(lost), lost_description(lost, "docker is not running")[1])
        self.assertEqual(title_for(job(status=SUCCESS, exit_code=0)), PASSED_DESCRIPTION)
        self.assertEqual(title_for(job(status=PENDING, device=None, attempts=0)), QUEUED_DESCRIPTION)

    def test_the_status_text_the_broker_posts_is_the_title_the_check_renders(self) -> None:
        # the two call sites are not this module's to edit, so the agreement is
        # held by asserting on the strings they actually send
        from cindral.jobapi import JobApiMixin
        from cindral.service import CindralHandler

        self.assertIn(PASSED_DESCRIPTION, _sent_literals(JobApiMixin._report))
        self.assertIn(QUEUED_DESCRIPTION, _sent_literals(CindralHandler._enqueue_direct))

    def test_the_check_is_named_after_the_status_context_it_replaces(self) -> None:
        import inspect

        default = inspect.signature(GitHubClient.post_status).parameters["context"].default
        self.assertEqual(DEFAULT_CHECK_NAME, default)


if __name__ == "__main__":
    unittest.main()

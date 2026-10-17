"""The expressions a job-level `if:` is written in, read the way they read.

Split from the reusable-workflow tests because a condition is read on its own,
without a call to read it through, and because the two together outgrew the file.
"""
import tempfile
import unittest
from pathlib import Path

from cindral.jobcondition import ConditionError
from cindral.actions import (
    WorkflowError,
    WorkflowNoMatch,
    WorkflowUnsupported,
    resolve_workflow_job,
)
from cindral.agent import JobSpec
from cindral.workflow_context import RunContext


LEOTRON_CI = """
name: CI

on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  validate:
    if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository
    runs-on: [self-hosted, Linux, ARM64, leotron]
    timeout-minutes: 60

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: 24

      - name: Setup pnpm
        uses: pnpm/action-setup@v4

      - name: Install dependencies
        run: pnpm install --frozen-lockfile
"""

class JobConditionTest(unittest.TestCase):
    """A job-level `if`, evaluated for the event the run was queued for.

    The workflow is `example-org/web-app`'s, with the trigger the repository
    has added so the job is reachable by the events Cindral dispatches. The
    condition is the one that repository wrote, unchanged.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.workflows = self.repo / ".github" / "workflows"
        self.workflows.mkdir(parents=True)
        (self.workflows / "ci.yml").write_text(
            LEOTRON_CI.replace("on:\n  workflow_dispatch:", "on: [push, pull_request]"),
            encoding="utf-8",
        )

    def job(self, ref: str = "refs/heads/main") -> JobSpec:
        return JobSpec("job1", "example-org/web-app", "a" * 40, ref, (), 3600)

    def test_a_pull_request_the_condition_cannot_decide_is_reported_not_ignored(self) -> None:
        # the queue record does not say which repository opened the pull
        # request, so running the job would report a build for work the
        # repository's own condition may have declined
        with self.assertRaises(WorkflowUnsupported) as cm:
            resolve_workflow_job(self.repo, self.job(ref="refs/pull/7/merge"))
        message = str(cm.exception)
        self.assertIn("head.repo.full_name", message)
        self.assertIn("must decide", message)

    def test_a_condition_every_matching_job_is_skipped_by_is_reported(self) -> None:
        (self.workflows / "ci.yml").write_text("""
name: ci
on: [push]
jobs:
  release:
    if: github.ref == 'refs/tags/v1'
    runs-on: ubuntu-latest
    steps:
      - run: echo released
""", encoding="utf-8")
        with self.assertRaises(WorkflowError) as cm:
            resolve_workflow_job(self.repo, self.job())
        message = str(cm.exception)
        self.assertIn("skipped by its own if", message)
        # a decision the repository made, so it is reported rather than
        # replaced by a lockfile run of something else
        self.assertNotIsInstance(cm.exception, WorkflowNoMatch)

    def test_a_condition_that_holds_selects_the_job(self) -> None:
        (self.workflows / "ci.yml").write_text("""
name: ci
on: [push]
jobs:
  check:
    if: github.event_name == 'push'
    runs-on: ubuntu-latest
    steps:
      - run: make check
""", encoding="utf-8")
        _, job = resolve_workflow_job(self.repo, self.job())
        self.assertEqual(job.id, "check")


class ConditionReadingTest(unittest.TestCase):
    """The expressions a job condition is written in, read the way they read."""

    def condition(self, expression: str, ref: str = "refs/heads/main", event: str = "push"):
        from cindral.jobcondition import evaluate_condition
        from cindral.workflow_context import RunContext

        spec = JobSpec("job1", "org/repo", "a" * 40, ref, (), 3600)
        return evaluate_condition(expression, RunContext.from_job(spec, event))

    def test_a_comparison_of_two_values_the_run_carries_is_decided(self) -> None:
        self.assertTrue(self.condition("github.ref == 'refs/heads/main'").value)
        self.assertFalse(self.condition("github.ref == 'refs/heads/other'").value)

    def test_a_boolean_operator_short_circuits_before_the_unreadable_side(self) -> None:
        # this is the shape both fleet repositories write, and reading the
        # second operand on a push would refuse a condition that is already
        # decided by the first
        condition = self.condition(
            "github.event_name != 'pull_request' "
            "|| github.event.pull_request.head.repo.full_name == github.repository"
        )
        self.assertTrue(condition.value)
        self.assertEqual(condition.unread, ())

    def test_repeated_parentheses_are_that_operand(self) -> None:
        """`((x))` is `x`, at any depth, and the first fix only handled one level.

        A check on the next two tokens read the inner group of `((0))` as a
        condition, so `0` became the string "true" and ran a job GitHub skips,
        while `((github.ref)) == 'x'` compared a verdict against a ref and skipped
        a job GitHub runs. `((false))` happened to be right, which is what made it
        invisible to a spot check.
        """
        self.assertFalse(self.condition("((0))").value)
        self.assertTrue(self.condition("((1))").value)
        self.assertFalse(self.condition("((false))").value)
        self.assertTrue(self.condition("((github.ref)) == 'refs/heads/main'").value)
        self.assertFalse(self.condition("(((github.ref))) == 'refs/heads/other'").value)

    def test_a_string_comparison_ignores_case(self) -> None:
        # GitHub compares strings without regard to case, so `PUSH` is the event
        # the run queued for
        self.assertTrue(self.condition("github.event_name == 'PUSH'").value)
        self.assertTrue(self.condition("github.ref_name == 'MAIN'").value)

    def test_two_values_are_compared_as_numbers_when_both_read_as_numbers(self) -> None:
        # GitHub coerces both sides to a number, so an empty string is zero
        self.assertTrue(self.condition("0 == ''").value)
        self.assertTrue(self.condition("1 == 1.0").value)
        self.assertFalse(self.condition("1 == ''").value)

    def test_a_guard_nested_past_the_bound_is_refused_rather_than_crashing(self) -> None:
        """The four read functions recurse, and a RecursionError is not a refusal.

        It is a RuntimeError, so it escaped the handler that turns a condition it
        cannot read into a WorkflowUnsupported, and the job died as an unexpected
        executor failure instead of reaching its committed contract.
        """
        with self.assertRaises(ConditionError) as cm:
            self.condition("(" * 300 + "github.ref" + ")" * 300)
        self.assertIn("nests deeper", str(cm.exception))
        # and a depth a person would write is still read
        self.assertTrue(self.condition("(" * 8 + "github.ref" + ")" * 8).value)

    def test_a_long_negation_chain_is_refused_rather_than_crashing(self) -> None:
        """A `!` chain never reaches the parenthesis bound, so it needs its own.

        `_not` recurses on itself and only reaches `_operand` once every `!` is
        consumed, so a bound on the other two descents fired at whatever depth
        the interpreter happened to survive to. A thousand negations is one
        quoted line in a committed workflow, and it arrived as a RecursionError:
        a RuntimeError, which no refusal handler catches, so the job died
        unnamed and never reached its contract.
        """
        with self.assertRaises(ConditionError) as cm:
            self.condition("!" * 1000 + "true")
        self.assertIn("nests deeper", str(cm.exception))
        # and a chain a person would write is still read
        self.assertFalse(self.condition("!!!true").value)
        self.assertTrue(self.condition("!false").value)
        self.assertFalse(self.condition("!!!!false").value)

    def test_a_negative_number_is_a_number(self) -> None:
        # a number default Cindral itself writes has to be readable by Cindral,
        # and `-1` with no sign in the pattern was a '-' the tokenizer refused
        self.assertTrue(self.condition("-1").value)
        self.assertTrue(self.condition("-1 == -1").value)
        self.assertFalse(self.condition("-1 == 1").value)

    def test_two_sides_of_one_type_compare_as_that_type(self) -> None:
        """Same type, same comparison. Different type, both coerced to numbers.

        Comparing two strings numerically made `'1' == '1.0'` true, where GitHub
        compares them as the strings they are. And a mixed comparison coerces: a
        boolean becomes one or zero, and a value that will not coerce is not equal
        to anything, which is what makes `(true) == 'true'` false rather than
        true.
        """
        self.assertFalse(self.condition("'1' == '1.0'").value)
        self.assertFalse(self.condition("'1' == '01'").value)
        self.assertFalse(self.condition("'10' == '1e1'").value)
        self.assertTrue(self.condition("true == 1").value)
        self.assertFalse(self.condition("(true) == 'true'").value)
        self.assertFalse(self.condition("true == 'true'").value)
        self.assertFalse(self.condition(
            "(github.ref == 'refs/heads/main') == 'true'"
        ).value)

    def test_two_booleans_compare_as_booleans(self) -> None:
        self.assertTrue(self.condition("true == true").value)
        self.assertFalse(self.condition("true == false").value)

    def test_a_doubled_quote_inside_a_literal_is_one_quote(self) -> None:
        # GitHub escapes a quote in a string by doubling it, and a default
        # substituted into a guard brings one with it
        self.assertTrue(self.condition("'it''s' == 'IT''S'").value)

    def test_a_parenthesised_value_is_compared_as_a_value(self) -> None:
        """`(github.ref) == 'x'` compares the ref, not a verdict about it.

        Read as a condition, the group yields a bool, and a bool is then compared
        against the string on the right, which is never equal. So every
        parenthesised value read this way answered false where GitHub answers
        true, and a job GitHub runs was reported as skipped.
        """
        self.assertTrue(self.condition("(github.ref) == 'refs/heads/main'").value)
        self.assertTrue(self.condition("(github.event_name) == 'push'").value)
        self.assertTrue(self.condition("(github.ref) != 'refs/heads/other'").value)
        self.assertFalse(self.condition("(github.ref) == 'refs/heads/other'").value)

    def test_a_parenthesised_group_is_still_read_as_a_condition(self) -> None:
        # the same shape without a comparison is a condition, and stays one
        self.assertTrue(self.condition("(github.ref == 'refs/heads/main')").value)
        self.assertFalse(self.condition("(github.ref == 'refs/heads/other')").value)

    def test_a_bare_number_is_false_only_at_zero(self) -> None:
        # its text is non-empty at zero, and truthiness of text is the wrong
        # question for a number
        self.assertFalse(self.condition("0").value)
        self.assertFalse(self.condition("0.0").value)
        self.assertTrue(self.condition("1").value)

    def test_a_bare_true_short_circuits_the_side_it_does_not_reach(self) -> None:
        """`true || anything` settles on the `true`.

        The literal was carried as the string "true" and the other side as a real
        bool, so the comparison never matched, the condition came back
        undecided, and a job the workflow marked unconditional was refused by
        name for a field the run does not carry.
        """
        condition = self.condition("true || github.event.pull_request.head.repo.full_name")
        self.assertTrue(condition.value)
        self.assertEqual(condition.unread, ())

    def test_a_bare_false_settles_an_and_without_its_other_side(self) -> None:
        condition = self.condition("false && github.event.pull_request.head.repo.full_name")
        self.assertFalse(condition.value)
        self.assertEqual(condition.unread, ())

    def test_a_bare_literal_reads_as_the_boolean_it_names(self) -> None:
        # the value on Condition is annotated bool, so a string there was a lie
        # every future consumer would have inherited
        self.assertIs(self.condition("true").value, True)
        self.assertIs(self.condition("false").value, False)
        self.assertIs(self.condition("null").value, False)

    def test_a_pull_request_where_the_second_side_decides_is_left_undecided(self) -> None:
        condition = self.condition(
            "github.event_name == 'pull_request' "
            "&& github.event.pull_request.head.repo.full_name == github.repository",
            ref="refs/pull/7/merge",
            event="pull_request",
        )
        self.assertIsNone(condition.value)
        self.assertEqual(condition.unread, ("github.event.pull_request.head.repo.full_name",))

    def test_an_and_does_not_reach_a_side_it_does_not_need(self) -> None:
        condition = self.condition("github.ref == 'refs/heads/other' && github.actor == 'example-org'")
        self.assertFalse(condition.value)

    def test_a_negation_of_an_unreadable_path_stays_undecided(self) -> None:
        condition = self.condition("!github.actor")
        self.assertIsNone(condition.value)
        self.assertEqual(condition.unread, ("github.actor",))

    def test_a_parenthesised_condition_is_read(self) -> None:
        self.assertTrue(self.condition("(github.ref == 'refs/heads/main')").value)

    def test_a_literal_is_a_value_rather_than_a_context_read(self) -> None:
        self.assertTrue(self.condition("true").value)
        self.assertFalse(self.condition("false").value)
        self.assertEqual(self.condition("'a' == 'a'").value, True)

    def test_a_function_call_is_refused_by_name(self) -> None:
        from cindral.jobcondition import ConditionError

        with self.assertRaises(ConditionError) as cm:
            self.condition("contains(github.ref, 'main')")
        self.assertIn("unreadable text", str(cm.exception))

    def test_a_bare_path_is_a_condition_read_for_its_truth(self) -> None:
        condition = self.condition("github.ref")
        self.assertTrue(condition.value)
        self.assertEqual(condition.unread, ())

    def test_a_bare_path_the_run_does_not_carry_is_left_undecided(self) -> None:
        condition = self.condition("github.actor")
        self.assertIsNone(condition.value)
        self.assertEqual(condition.unread, ("github.actor",))


if __name__ == "__main__":
    unittest.main()

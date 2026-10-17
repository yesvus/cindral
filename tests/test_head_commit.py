"""Which commit a run's result is recorded against, and that nothing re-derives it.

Kept apart from the check-run publisher because the question is not how a check is
written but which commit it is written about. The rule has one definition, four
callers, and two shapes of run: a push tests and reports one commit, and a pull
request tests the merge ref while reporting on the branch head, so the two differ by
design and a differing head is not a fault to refuse.
"""
import unittest
from pathlib import Path

import cindral
from cindral.check_summary import summary_for
from cindral.jobrecord import head_sha_for

from test_check_runs import SHA, job


class HeadCommitTest(unittest.TestCase):
    """Which commit a run's result is recorded against, stated once and asserted.

    Four places used to spell the rule out as `status_sha or sha`, and a fifth
    would have spelled it out the same way and resolved a commit fresh. That is the
    failure this issue was filed about: a verdict written against whatever the branch
    pointed at by the time it was published is attributed to a commit nobody tested,
    and nothing afterwards distinguishes it from a correct one.

    So the rule has one definition, the callers read it from there, and the shape of
    each kind of run is pinned rather than assumed. A pull request tests the merge
    ref and reports on the branch head, so the two differ by design and the head must
    never be refused as a wrong commit.
    """

    def modules(self) -> dict[str, str]:
        return {
            path.name: path.read_text(encoding="utf-8")
            for path in Path(cindral.__file__).parent.glob("*.py")
        }

    def test_a_push_reports_against_the_commit_it_tested(self) -> None:
        self.assertEqual(head_sha_for(SHA), SHA)
        # a push that recorded a head equal to its sha is the same commit, not a
        # second opinion about which one to write to
        self.assertEqual(head_sha_for(SHA, SHA), SHA)

    def test_a_pull_request_reports_against_the_head_it_did_not_test(self) -> None:
        self.assertEqual(head_sha_for("m" * 40, "h" * 40), "h" * 40)

    def test_the_rule_is_not_re_derived_anywhere(self) -> None:
        """One definition, so a fifth place cannot resolve a commit on its own."""
        # the definition is excluded because naming the rule is its job, and its
        # docstring has to be able to quote it
        for name, text in self.modules().items():
            if name == "jobrecord.py":
                continue
            with self.subTest(module=name):
                self.assertNotIn(
                    "status_sha or ", text,
                    f"{name} re-derives which commit a result is recorded against",
                )

    def test_every_caller_reads_the_rule_from_the_one_definition(self) -> None:
        # named rather than counted, so a fourth caller is a visible addition
        callers = {
            name: text for name, text in self.modules().items()
            if "head_sha_for(" in text and name != "jobrecord.py"
        }
        self.assertEqual(
            sorted(callers),
            ["check_runs.py", "jobapi.py", "lease_reclaim.py", "service.py"],
        )
        for name, text in callers.items():
            with self.subTest(module=name):
                self.assertIn("from .jobrecord import", text, f"{name} does not import the rule")

    def test_a_push_is_not_given_a_head_it_does_not_differ_from(self) -> None:
        """The head is named only when it is a different commit.

        Naming it unconditionally makes every push read "built X, head X", and the
        reader can no longer tell a pull request from a push by whether a second
        commit appears. The difference is the whole information in the line.
        """
        # the annotation, not the word: every branch ref reads refs/heads/...
        for summary in (
            summary_for(job(sha=SHA, status_sha=SHA, ref="refs/heads/main")),
            summary_for(job(sha=SHA, ref="refs/heads/main")),
        ):
            with self.subTest(ref=summary.splitlines()[1][:40]):
                self.assertIn(SHA, summary)
                self.assertNotIn("head `", summary)

    def test_the_published_check_names_both_commits(self) -> None:
        """A reader must be able to check the attribution, not trust it.

        The head is correct and unexplained often enough that a check showing only
        one commit leaves the reader unable to tell a pull request from a
        misattribution. Both are named: which tree ran, and which commit the result
        is recorded against.
        """
        summary = summary_for(job(sha="m" * 40, status_sha="h" * 40, ref="refs/pull/1/merge"))
        self.assertIn("m" * 40, summary)
        self.assertIn("h" * 40, summary)

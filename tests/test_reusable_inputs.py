"""An input a reusable workflow declares, read from both ends of the call.

The caller's own `if:` reads the inputs the caller declares, and the called
workflow's reads what the call supplies or what it defaulted to. One value ends up
in two places that need it in two forms, which is what this file is about.

Split from the reusable-workflow tests because an input is a property of the
`on:` block rather than of the call, and because the two together outgrew the file.
"""
import tempfile
import unittest
from pathlib import Path

from cindral.actions import WorkflowError, resolve_workflow_job
from cindral.agent import JobSpec
from cindral.discovery import parse_workflow
from cindral.workflow_errors import WorkflowUnsupported

from test_reusable import BIRTEDCOM_CI, BIRTEDCOM_JOBS

REPO = "example-org/store"


class ReusableInputTest(unittest.TestCase):
    """The caller's inputs, the callee's defaults, and the forms each needs.

    The same fixtures and the same repository layout as the reusable-workflow
    tests, declared rather than inherited: an imported TestCase subclass is
    collected by discovery and its cases run twice.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.workflows = self.repo / ".github" / "workflows"
        self.workflows.mkdir(parents=True)
        (self.workflows / "ci.yml").write_text(BIRTEDCOM_CI, encoding="utf-8")
        (self.workflows / "ci-jobs.yml").write_text(BIRTEDCOM_JOBS, encoding="utf-8")

    def job(self, ref: str = "refs/heads/master", target: str = "") -> JobSpec:
        return JobSpec("job1", REPO, "a" * 40, ref, (target,) if target else (), 3600)

    def test_a_caller_condition_reading_an_input_is_resolved_against_the_callers_own(self) -> None:
        """A caller's `if:` reads the caller's inputs, not the called workflow's.

        The expression arrives intact, and the condition parser then refuses a job
        whose guard was never resolved, so a repository guarding its call with
        `if: ${{ inputs.run-ci }}` could not be selected at all.
        """
        (self.workflows / "ci.yml").write_text("""
name: gated
on:
  workflow_dispatch:
    inputs:
      run-ci:
        type: boolean
        default: true

jobs:
  fork-ci:
    if: ${{ inputs.run-ci }}
    uses: ./.github/workflows/ci-jobs.yml
    with:
      runner: leotron
""", encoding="utf-8")
        workflow = parse_workflow(self.workflows / "ci.yml", self.repo)
        self.assertIn("fork-ci/lint", workflow.jobs)
        # the default resolves to true, so the guard is a truthy literal rather
        # than an expression nothing can read
        self.assertNotIn("${{", workflow.jobs["fork-ci/lint"].condition or "")

    def test_a_caller_condition_false_at_its_default_reaches_no_job(self) -> None:
        """The unsafe direction of resolving against the declared default.

        The translation happens before the run, so it sees the default and not
        the value the dispatch carries. A guard defaulting to true runs a job
        whose dispatch set it false, and a guard defaulting to false skips a job
        whose dispatch set it true. Both are the same approximation, and this is
        the one that costs a skip rather than a run.

        The first version of this test declared no `push` trigger, so the
        workflow never matched the event and the assertion held whether or not
        the substitution ran. It is the only test covering this direction, and it
        was not testing it.
        """
        (self.workflows / "ci.yml").write_text("""
name: gated
on:
  workflow_dispatch:
    inputs:
      run-ci:
        type: boolean
        default: false
  push:

jobs:
  fork-ci:
    if: ${{ inputs.run-ci }}
    uses: ./.github/workflows/ci-jobs.yml
    with:
      runner: leotron
""", encoding="utf-8")
        workflow = parse_workflow(self.workflows / "ci.yml", self.repo)
        self.assertEqual(workflow.jobs["fork-ci/lint"].condition, "false")
        # the guard is read, so the call brings in a job that is known to be
        # skipped. That is a different message from a workflow with no such job,
        # and it is the one that says the guard was the reason.
        with self.assertRaises(WorkflowError) as cm:
            resolve_workflow_job(self.repo, self.job(target="lint"))
        self.assertIn("skipped by its own if", str(cm.exception))
        self.assertIn("fork-ci/lint", str(cm.exception))

    def test_a_string_input_whose_default_reads_as_a_literal_is_quoted(self) -> None:
        """A `type: string` default of the text `false` is a true string.

        Substituted bare, the two characters became the literal `false` and
        skipped a job GitHub runs, because a non-empty string is true. Reached
        in practice through a `type: choice` input offering `[true, false]`,
        whose options are the strings.
        """
        (self.workflows / "ci.yml").write_text("""
name: gated
on:
  workflow_dispatch:
    inputs:
      run-ci:
        type: string
        default: 'false'
  push:

jobs:
  fork-ci:
    if: ${{ inputs.run-ci }}
    uses: ./.github/workflows/ci-jobs.yml
    with:
      runner: leotron
""", encoding="utf-8")
        workflow = parse_workflow(self.workflows / "ci.yml", self.repo)
        self.assertEqual(workflow.jobs["fork-ci/lint"].condition, "'false'")
        # a quoted non-empty string is true, so the call still brings its jobs in
        self.assertEqual(resolve_workflow_job(self.repo, self.job(target="lint"))[1].id, "fork-ci/lint")

    def test_a_number_input_defaulting_to_zero_skips_the_job(self) -> None:
        """A number reaching the guard as a number is false at zero.

        Quoted it would be the non-empty string '0', which is true, so an input
        counting to zero ran a job GitHub skips. The same bug as a string default
        of the text `false`, and left unfixed when that one was.
        """
        (self.workflows / "ci.yml").write_text("""
name: gated
on:
  workflow_dispatch:
    inputs:
      count:
        type: number
        default: 0
  push:

jobs:
  fork-ci:
    if: ${{ inputs.count }}
    uses: ./.github/workflows/ci-jobs.yml
    with:
      runner: leotron
""", encoding="utf-8")
        workflow = parse_workflow(self.workflows / "ci.yml", self.repo)
        self.assertEqual(workflow.jobs["fork-ci/lint"].condition, "0")
        with self.assertRaises(WorkflowError) as cm:
            resolve_workflow_job(self.repo, self.job(target="lint"))
        self.assertIn("skipped by its own if", str(cm.exception))

    def test_a_number_input_defaulting_above_zero_runs_the_job(self) -> None:
        (self.workflows / "ci.yml").write_text("""
name: gated
on:
  workflow_dispatch:
    inputs:
      count:
        type: number
        default: 3
  push:

jobs:
  fork-ci:
    if: ${{ inputs.count }}
    uses: ./.github/workflows/ci-jobs.yml
    with:
      runner: leotron
""", encoding="utf-8")
        workflow = parse_workflow(self.workflows / "ci.yml", self.repo)
        self.assertEqual(workflow.jobs["fork-ci/lint"].condition, "3")
        self.assertEqual(resolve_workflow_job(self.repo, self.job(target="lint"))[1].id, "fork-ci/lint")

    def test_a_number_input_that_names_no_number_is_refused_by_name(self) -> None:
        """A default declared a number must read as one, or be named as wrong.

        `float` raising here would be a bare ValueError, and a ValueError is not
        a WorkflowUnsupported, so it skipped the committed contract that exists
        to answer a default this cannot read, and the job died unnamed.
        """
        (self.workflows / "ci.yml").write_text("""
name: gated
on:
  workflow_dispatch:
    inputs:
      count:
        type: number
        default: 'seven'
  push:

jobs:
  fork-ci:
    if: ${{ inputs.count }}
    uses: ./.github/workflows/ci-jobs.yml
    with:
      runner: leotron
""", encoding="utf-8")
        with self.assertRaises(WorkflowUnsupported) as cm:
            parse_workflow(self.workflows / "ci.yml", self.repo)
        self.assertIn("seven", str(cm.exception))
        self.assertIn("not one", str(cm.exception))

    def test_a_boolean_input_default_is_still_a_bare_literal(self) -> None:
        (self.workflows / "ci.yml").write_text("""
name: gated
on:
  workflow_dispatch:
    inputs:
      run-ci:
        type: boolean
        default: true
  push:

jobs:
  fork-ci:
    if: ${{ inputs.run-ci }}
    uses: ./.github/workflows/ci-jobs.yml
    with:
      runner: leotron
""", encoding="utf-8")
        workflow = parse_workflow(self.workflows / "ci.yml", self.repo)
        self.assertEqual(workflow.jobs["fork-ci/lint"].condition, "true")

    def test_a_caller_condition_naming_an_undeclared_input_is_refused_by_name(self) -> None:
        # an input the caller does not declare cannot be resolved, and saying so
        # is better than leaving an expression the condition parser rejects
        (self.workflows / "ci.yml").write_text("""
name: gated
on:
  workflow_dispatch:
    inputs:
      other:
        default: 'true'

jobs:
  fork-ci:
    if: ${{ inputs.run-ci }}
    uses: ./.github/workflows/ci-jobs.yml
    with:
      runner: leotron
""", encoding="utf-8")
        with self.assertRaises(WorkflowUnsupported) as cm:
            parse_workflow(self.workflows / "ci.yml", self.repo)
        self.assertIn("run-ci", str(cm.exception))

    def test_a_called_workflow_uses_the_default_for_an_input_the_caller_omits(self) -> None:
        """The canonical shape: a reusable workflow that defaults what it reads.

        PyYAML resolves an `on:` key to the boolean True. `parse_workflow`
        compensates for that and `_read_call` did not, so the called workflow's
        declared defaults never loaded and every input it declared one for was
        refused by name as one the caller failed to pass, taking the whole
        repository with it. This is the fixture GitHub's own docs use.
        """
        (self.workflows / "ci.yml").write_text("""
name: caller
on:
  push:
jobs:
  call:
    uses: ./.github/workflows/defaulted.yml
""", encoding="utf-8")
        (self.workflows / "defaulted.yml").write_text("""
name: called
on:
  workflow_call:
    inputs:
      runner:
        type: string
        default: ubuntu-latest

jobs:
  build:
    runs-on: ${{ inputs.runner }}
    steps:
      - run: pnpm ci
""", encoding="utf-8")
        workflow = parse_workflow(self.workflows / "ci.yml", self.repo)
        self.assertIn("call/build", workflow.jobs)
        # the default is the bare text, because a `runs-on` reads it as a value
        self.assertEqual(workflow.jobs["call/build"].runs_on, ("ubuntu-latest",))

    def test_a_called_workflow_uses_the_caller_value_over_the_default(self) -> None:
        (self.workflows / "ci.yml").write_text("""
name: caller
on:
  push:
jobs:
  call:
    uses: ./.github/workflows/defaulted.yml
    with:
      runner: [self-hosted, Linux, ARM64, leotron]
""", encoding="utf-8")
        (self.workflows / "defaulted.yml").write_text("""
name: called
on:
  workflow_call:
    inputs:
      runner:
        type: string
        default: ubuntu-latest

jobs:
  build:
    runs-on: ${{ inputs.runner }}
    steps:
      - run: pnpm ci
""", encoding="utf-8")
        workflow = parse_workflow(self.workflows / "ci.yml", self.repo)
        self.assertEqual(
            workflow.jobs["call/build"].runs_on,
            ("['self-hosted', 'Linux', 'ARM64', 'leotron']",),
        )

    def test_a_called_string_input_defaulting_to_false_is_not_a_verdict(self) -> None:
        """One value, two forms: a `runs-on` reads text and a guard reads a type.

        Substituted as bare text, the string `false` became the boolean and
        skipped a job GitHub runs, because a non-empty string is true. The guard
        is rendered by the declared type and the rest of the job by the text.
        """
        for declared_type, default, runs in (("string", "'false'", True), ("boolean", "false", False)):
            with self.subTest(declared_type=declared_type):
                (self.workflows / "ci.yml").write_text("""
name: caller
on:
  push:
jobs:
  call:
    uses: ./.github/workflows/guarded.yml
""", encoding="utf-8")
                (self.workflows / "guarded.yml").write_text(f"""
name: called
on:
  workflow_call:
    inputs:
      flag:
        type: {declared_type}
        default: {default}

jobs:
  build:
    if: ${{{{ inputs.flag }}}}
    runs-on: ubuntu-24.04
    steps:
      - run: pnpm ci
""", encoding="utf-8")
                workflow = parse_workflow(self.workflows / "ci.yml", self.repo)
                if runs:
                    self.assertEqual(
                        resolve_workflow_job(self.repo, self.job(target="build"))[1].id,
                        "call/build",
                    )
                else:
                    with self.assertRaises(WorkflowError) as cm:
                        resolve_workflow_job(self.repo, self.job(target="build"))
                    self.assertIn("skipped by its own if", str(cm.exception))


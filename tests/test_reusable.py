"""A reusable workflow call and a job condition, read from the files that use them.

`example-org/store` enters CI through a call to another workflow in its own
repository, and `example-org/web-app` guards its job with a condition naming a
field the queue record does not carry. Both are transcribed from the files those
repositories commit.
"""
import tempfile
import unittest
from pathlib import Path

from cindral.actions import WorkflowError, WorkflowNoMatch, WorkflowUnsupported, resolve_workflow_job
from cindral.agent import JobSpec
from cindral.discovery import WorkflowUnsupported as ParseUnsupported, parse_workflow

# example-org/store, .github/workflows/ci.yml
BIRTEDCOM_CI = """
name: CI

on:
  push:
    branches: [master, staging]
  pull_request:
    branches: [master]

permissions:
  contents: read
  packages: read

concurrency:
  group: ${{ github.workflow }}-${{ github.event_name }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  fork-ci:
    if: github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name != github.repository
    uses: ./.github/workflows/ci-jobs.yml
    with:
      runner: ubuntu-latest
"""

# example-org/store, .github/workflows/ci-jobs.yml
BIRTEDCOM_JOBS = """
name: CI Jobs

on:
  workflow_call:
    inputs:
      runner:
        description: Runner label for the CI jobs
        required: true
        type: string

permissions:
  contents: read
  packages: read

env:
  NODE_VERSION: '22'

jobs:
  lint:
    name: Lint
    runs-on: ${{ inputs.runner }}
    steps:
      - uses: actions/checkout@v7
      - uses: pnpm/action-setup@v6
      - uses: actions/setup-node@v7
        with:
          node-version: ${{ env.NODE_VERSION }}
          cache: pnpm
      - run: pnpm install --frozen-lockfile
      - run: pnpm lint

  typecheck:
    name: Type Check
    runs-on: ${{ inputs.runner }}
    steps:
      - uses: actions/checkout@v7
      - run: pnpm typecheck

  build:
    name: Build
    runs-on: ${{ inputs.runner }}
    needs: [lint, typecheck]
    steps:
      - uses: actions/checkout@v7
      - run: pnpm build
"""

# example-org/web-app, .github/workflows/ci.yml


class ReusableWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.workflows = self.repo / ".github" / "workflows"
        self.workflows.mkdir(parents=True)
        (self.workflows / "ci.yml").write_text(BIRTEDCOM_CI, encoding="utf-8")
        (self.workflows / "ci-jobs.yml").write_text(BIRTEDCOM_JOBS, encoding="utf-8")

    def job(self, ref: str = "refs/heads/master", target: str = "") -> JobSpec:
        return JobSpec("job1", "example-org/store", "a" * 40, ref, (target,) if target else (), 3600)

    def test_the_call_produces_the_jobs_of_the_workflow_it_calls(self) -> None:
        # a call is the repository's entry point, so a translation that read
        # only the file the run names would find a job with no steps at all
        workflow = parse_workflow(self.workflows / "ci.yml", self.repo)
        self.assertEqual(
            sorted(workflow.jobs),
            ["fork-ci/build", "fork-ci/lint", "fork-ci/typecheck"],
        )

    def test_an_inlined_job_carries_the_runner_the_caller_passed(self) -> None:
        # `runs-on: ${{ inputs.runner }}` is the called workflow asking for the
        # label, which the caller supplies
        workflow = parse_workflow(self.workflows / "ci.yml", self.repo)
        self.assertEqual(workflow.jobs["fork-ci/lint"].runs_on, ("ubuntu-latest",))

    def test_an_inlined_job_carries_the_called_workflow_environment(self) -> None:
        # the called workflow's own env reaches its jobs, which is where the
        # version its setup step reads comes from
        workflow = parse_workflow(self.workflows / "ci.yml", self.repo)
        self.assertEqual(workflow.jobs["fork-ci/lint"].env["NODE_VERSION"], "22")

    def test_an_inlined_job_carries_the_caller_condition(self) -> None:
        # the caller decides whether the call happens at all, so the jobs it
        # brings in are gated the same way
        workflow = parse_workflow(self.workflows / "ci.yml", self.repo)
        self.assertIn("github.event_name == 'pull_request'", workflow.jobs["fork-ci/lint"].condition)

    def select(self, target: str):
        """Select an inlined job from a call the repository has not gated."""
        (self.workflows / "ci.yml").write_text(BIRTEDCOM_CI.replace(
            "    if: github.event_name == 'pull_request'"
            " && github.event.pull_request.head.repo.full_name != github.repository\n", ""
        ), encoding="utf-8")
        return resolve_workflow_job(self.repo, self.job(target=target))

    def test_an_inlined_job_is_selectable_by_the_name_it_has_inside(self) -> None:
        # a maintainer reads `lint` in the dispatch, not `fork-ci/lint`
        _, job = self.select("lint")
        self.assertEqual(job.id, "fork-ci/lint")

    def test_an_inlined_job_is_also_selectable_by_its_qualified_name(self) -> None:
        _, job = self.select("fork-ci/lint")
        self.assertEqual(job.id, "fork-ci/lint")

    def test_a_pull_request_the_caller_condition_cannot_decide_reaches_no_job(self) -> None:
        # a pull request is the only event the call is for, and deciding it
        # needs a field the record does not carry
        with self.assertRaises(WorkflowUnsupported) as cm:
            resolve_workflow_job(self.repo, self.job(target="lint", ref="refs/pull/7/merge"))
        self.assertIn("head.repo.full_name", str(cm.exception))

    def test_a_bare_boolean_in_the_called_workflow_is_read_too(self) -> None:
        """The same guard, one level down, which is where it was still dropped.

        The caller's own `if:` goes through the boolean read, so `if: false` on a
        caller skips its call. The *called* workflow's `if:` did not, and a
        `False` there fell through the combination to no guard at all, so the job
        the reusable workflow explicitly skips ran on every push.
        """
        (self.workflows / "ci.yml").write_text("""
name: caller
on:
  workflow_dispatch:
  push:
jobs:
  fork-ci:
    uses: ./.github/workflows/ci-jobs.yml
    with:
      runner: leotron
""", encoding="utf-8")
        called = BIRTEDCOM_JOBS.replace(
            "  lint:\n    name: Lint", "  lint:\n    if: false\n    name: Lint", 1
        )
        (self.workflows / "ci-jobs.yml").write_text(called, encoding="utf-8")
        workflow = parse_workflow(self.workflows / "ci.yml", self.repo)
        self.assertEqual(workflow.jobs["fork-ci/lint"].condition, "false")
        with self.assertRaises(WorkflowError) as cm:
            resolve_workflow_job(self.repo, self.job(target="lint"))
        self.assertIn("skipped by its own if", str(cm.exception))

    def test_a_called_boolean_guard_is_combined_rather_than_replaced(self) -> None:
        # a caller guard and a called guard are two conditions, and the called
        # one being a bare `true` must not overwrite the caller's
        (self.workflows / "ci.yml").write_text("""
name: caller
on:
  workflow_dispatch:
  push:
jobs:
  fork-ci:
    if: github.event_name == 'push'
    uses: ./.github/workflows/ci-jobs.yml
    with:
      runner: leotron
""", encoding="utf-8")
        called = BIRTEDCOM_JOBS.replace(
            "  lint:\n    name: Lint", "  lint:\n    if: true\n    name: Lint", 1
        )
        (self.workflows / "ci-jobs.yml").write_text(called, encoding="utf-8")
        workflow = parse_workflow(self.workflows / "ci.yml", self.repo)
        self.assertEqual(
            workflow.jobs["fork-ci/lint"].condition,
            "(github.event_name == 'push') && (true)",
        )

    def test_a_call_to_a_workflow_that_is_a_symlink_loop_is_named(self) -> None:
        # resolve() raises RuntimeError here, not OSError, and it escaped every
        # handler: the job died as an unexpected executor failure rather than
        # reaching the committed contract this refusal exists to reach
        (self.workflows / "loop.yml").symlink_to("loop.yml")
        (self.workflows / "ci.yml").write_text("""
name: caller
on: [push]
jobs:
  fork-ci:
    uses: ./.github/workflows/loop.yml
""", encoding="utf-8")
        with self.assertRaises(WorkflowUnsupported) as cm:
            parse_workflow(self.workflows / "ci.yml", self.repo)
        self.assertIn("loop.yml", str(cm.exception))

    def test_a_workflow_that_calls_itself_is_refused_by_name(self) -> None:
        (self.workflows / "loop.yml").write_text("""
name: loop
on: [push]
jobs:
  again:
    uses: ./.github/workflows/loop.yml
""", encoding="utf-8")
        with self.assertRaises(ParseUnsupported) as cm:
            parse_workflow(self.workflows / "loop.yml", self.repo)
        self.assertIn("already being read", str(cm.exception))

    def test_a_call_outside_the_repository_is_refused_by_name(self) -> None:
        (self.workflows / "remote.yml").write_text("""
name: remote
on: [push]
jobs:
  ci:
    uses: someone/else/.github/workflows/ci.yml@main
""", encoding="utf-8")
        with self.assertRaises(ParseUnsupported) as cm:
            parse_workflow(self.workflows / "remote.yml", self.repo)
        self.assertIn("not in this repository", str(cm.exception))

    def test_a_call_reaching_outside_the_repository_is_refused_by_name(self) -> None:
        (self.workflows / "escape.yml").write_text("""
name: escape
on: [push]
jobs:
  ci:
    uses: ./../../elsewhere/ci.yml
""", encoding="utf-8")
        with self.assertRaises(ParseUnsupported) as cm:
            parse_workflow(self.workflows / "escape.yml", self.repo)
        self.assertIn("outside the repository", str(cm.exception))

    def test_the_call_gates_every_job_it_brings_in(self) -> None:
        # the caller decides whether the call happens at all, so on a push,
        # where this condition is false, none of the called jobs runs
        with self.assertRaises(WorkflowError) as cm:
            resolve_workflow_job(self.repo, self.job())
        self.assertIn("skipped by its own if", str(cm.exception))

    def test_a_call_the_caller_does_not_supply_an_input_for_is_refused_by_name(self) -> None:
        (self.workflows / "missing.yml").write_text("""
name: missing
on: [push]
jobs:
  ci:
    uses: ./.github/workflows/needs-input.yml
""", encoding="utf-8")
        (self.workflows / "needs-input.yml").write_text("""
name: needs input
on:
  workflow_call:
    inputs:
      runner:
        required: true
        type: string
jobs:
  lint:
    runs-on: ${{ inputs.runner }}
    steps:
      - run: pnpm lint
""", encoding="utf-8")
        with self.assertRaises(ParseUnsupported) as cm:
            parse_workflow(self.workflows / "missing.yml", self.repo)
        self.assertIn("input 'runner'", str(cm.exception))
        self.assertIn("does not pass", str(cm.exception))

    def test_a_call_declaring_a_construct_cindral_does_not_run_is_refused_by_name(self) -> None:
        (self.workflows / "needs.yml").write_text("""
name: needs
on: [push]
jobs:
  ci:
    needs: [other]
    uses: ./.github/workflows/ci-jobs.yml
    with:
      runner: ubuntu-latest
""", encoding="utf-8")
        with self.assertRaises(ParseUnsupported) as cm:
            parse_workflow(self.workflows / "needs.yml", self.repo)
        self.assertIn("job dependencies", str(cm.exception))

    def test_a_job_the_called_workflow_gates_by_needs_is_still_refused(self) -> None:
        # inlining does not make `needs:` runnable: the dependency is a
        # property of the run, not of the translation
        workflow = parse_workflow(self.workflows / "ci.yml", self.repo)
        self.assertEqual(workflow.jobs["fork-ci/build"].unmapped, ("needs",))

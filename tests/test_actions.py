import tempfile
import unittest
from pathlib import Path

from cindral.act import ACT_BINARY, ACT_IMAGE
from cindral.actions import (
    DEFAULT_RUNNER_IMAGE,
    IMAGE_MAP,
    WorkflowError,
    event_matches,
    find_workflows,
    parse_workflow,
    resolve_workflow_contract,
    resolve_workflow_job,
    workflow_to_contract,
)
from cindral.agent import JobSpec
from cindral.workflow_context import RunContext, create_event_payload


def context_for(job: JobSpec, event_name: str = "push", **overrides) -> RunContext:
    return RunContext.from_job(job, event_name, **overrides)


class ActionsWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.workflows_dir = self.repo / ".github" / "workflows"
        self.workflows_dir.mkdir(parents=True)

    def context(self, ref: str = "refs/heads/main", event_name: str = "push") -> RunContext:
        job = JobSpec("job1", "org/repo", "sha123", ref, (), 60)
        return context_for(job, event_name, workflow="CI", job_id="test")

    def test_parses_workflow_with_steps_env_and_services(self) -> None:
        workflow_file = self.workflows_dir / "ci.yml"
        workflow_file.write_text(
            """
name: Integration
env:
  GLOBAL_VAR: "global"
on:
  push:
    branches: [main, "release/**"]
jobs:
  test:
    name: Run Test Suite
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    env:
      LOCAL_VAR: "local"
    services:
      redis:
        image: redis:7
        env:
          REDIS_PORT: "6379"
    steps:
      - uses: actions/checkout@v4
      - name: Run tests
        run: pytest -v
"""
        )
        wf = parse_workflow(workflow_file)
        self.assertEqual(wf.name, "Integration")
        self.assertIn("test", wf.jobs)
        job = wf.jobs["test"]
        self.assertEqual(job.name, "Run Test Suite")
        self.assertEqual(job.runs_on, ("ubuntu-24.04",))
        self.assertEqual(job.timeout_minutes, 20)
        self.assertEqual(job.env, {"GLOBAL_VAR": "global", "LOCAL_VAR": "local"})
        self.assertEqual(len(job.services), 1)
        self.assertEqual(job.services[0].name, "redis")
        self.assertEqual(job.services[0].image, "redis:7")

    def test_invalid_timeout_minutes_raises_error(self) -> None:
        workflow_file = self.workflows_dir / "bad-timeout.yml"
        workflow_file.write_text(
            """
name: Bad Timeout
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: -5
    steps:
      - run: echo hi
"""
        )
        with self.assertRaises(WorkflowError):
            parse_workflow(workflow_file)

    def test_event_matching_for_push_and_pull_request(self) -> None:
        on_spec = {
            "push": {"branches": ["main", "feature/*"]},
            "pull_request": {"branches": "main"},
            "workflow_dispatch": None,
        }
        self.assertTrue(event_matches(on_spec, "push", "refs/heads/main"))
        self.assertTrue(event_matches(on_spec, "push", "refs/heads/feature/auth"))
        self.assertFalse(event_matches(on_spec, "push", "refs/heads/bugfix/123"))
        self.assertFalse(event_matches(on_spec, "push", "refs/heads/other/merge"))
        self.assertTrue(event_matches(on_spec, "pull_request", "refs/pull/42/merge"))
        self.assertTrue(event_matches(on_spec, "pull_request", "pull/42/head"))
        self.assertFalse(event_matches(on_spec, "pull_request", "refs/pull/42/merge", base_ref="release"))
        self.assertTrue(event_matches(on_spec, "workflow_dispatch", "refs/heads/main"))
        self.assertFalse(event_matches(on_spec, "schedule", "refs/heads/main"))

    def test_tag_filtering_and_tag_only_workflows(self) -> None:
        tag_spec = {"push": {"tags": "v*"}}
        self.assertTrue(event_matches(tag_spec, "push", "refs/tags/v1.0.0"))
        self.assertFalse(event_matches(tag_spec, "push", "refs/tags/release-1.0"))
        self.assertFalse(event_matches(tag_spec, "push", "refs/heads/main"))

        tag_ignore_spec = {"push": {"tags-ignore": ["v*"]}}
        self.assertTrue(event_matches(tag_ignore_spec, "push", "refs/tags/release-1.0"))
        self.assertFalse(event_matches(tag_ignore_spec, "push", "refs/tags/v1.0.0"))
        self.assertFalse(event_matches(tag_ignore_spec, "push", "refs/heads/main"))

    def test_scalar_branch_and_tag_patterns(self) -> None:
        spec = {"push": {"branches": "develop", "tags": "v*"}}
        self.assertTrue(event_matches(spec, "push", "refs/heads/develop"))
        self.assertFalse(event_matches(spec, "push", "refs/heads/main"))
        self.assertTrue(event_matches(spec, "push", "refs/tags/v2.0"))
        self.assertFalse(event_matches(spec, "push", "refs/tags/beta"))

    def test_workflow_to_contract_rejects_unsupported_runs_on(self) -> None:
        workflow_file = self.workflows_dir / "windows.yml"
        workflow_file.write_text(
            """
name: Windows
on: [push]
jobs:
  win:
    runs-on: windows-latest
    steps:
      - run: echo hello
"""
        )
        wf = parse_workflow(workflow_file)
        with self.assertRaises(WorkflowError) as cm:
            workflow_to_contract(wf.jobs["win"], self.context())
        self.assertIn("the device pool runs linux/amd64", str(cm.exception))

    def test_a_dispatch_naming_a_single_job_file_selects_that_job(self) -> None:
        """A target may be the file's own name, when the file holds one job.

        A dispatch reads `ci.yml` because that is what the file is called, and the
        file names one job, so the name is unambiguous. This clause was dropped
        by the target-matching refactor and nothing pinned it: a dispatch that
        used to select the job began reporting that no job matched, with a
        message about matching when the real answer was a name.
        """
        (self.workflows_dir / "ci.yml").write_text("""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-24.04
    steps:
      - run: pnpm ci
""", encoding="utf-8")
        spec = JobSpec("job1", "org/repo", "a" * 40, "refs/heads/main", ("ci.yml",), 60)
        self.assertEqual(resolve_workflow_job(self.repo, spec)[1].id, "test")

    def test_a_dispatch_naming_a_multi_job_file_still_needs_the_job(self) -> None:
        # with two jobs the file's name is not enough, which is the other half
        # of the clause
        (self.workflows_dir / "ci.yml").write_text("""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-24.04
    steps:
      - run: pnpm ci
  other:
    runs-on: ubuntu-24.04
    steps:
      - run: pnpm lint
""", encoding="utf-8")
        spec = JobSpec("job1", "org/repo", "a" * 40, "refs/heads/main", ("ci.yml",), 60)
        with self.assertRaises(WorkflowError) as cm:
            resolve_workflow_job(self.repo, spec)
        self.assertIn("for target 'ci.yml'", str(cm.exception))

    def test_a_job_guarded_by_a_bare_false_never_runs(self) -> None:
        """`if: false` is the one guard a repository writes as a YAML boolean.

        Read as a boolean and then dropped, it becomes no guard at all, and the
        job runs on every push. That is the one way to be wrong here that costs a
        run rather than skipping one.
        """
        (self.workflows_dir / "ci.yml").write_text("""
name: Gated
on: [push]
jobs:
  test:
    if: false
    runs-on: ubuntu-24.04
    steps:
      - run: pnpm ci
  always:
    if: true
    runs-on: ubuntu-24.04
    steps:
      - run: pnpm ci
""", encoding="utf-8")
        workflow = parse_workflow(self.workflows_dir / "ci.yml", self.repo)
        self.assertEqual(workflow.jobs["test"].condition, "false")
        self.assertEqual(workflow.jobs["always"].condition, "true")
        job = JobSpec("job1", "org/repo", "sha123", "refs/heads/main", ("test",), 60)
        # the guard is read, so the job is known to be skipped rather than
        # unknown: a workflow that says "this job never runs" is not the same as
        # a workflow with no such job, and the message says which
        with self.assertRaises(WorkflowError) as cm:
            resolve_workflow_job(self.repo, job)
        self.assertIn("skipped by its own if", str(cm.exception))
        self.assertIn("test", str(cm.exception))
        other = JobSpec("job1", "org/repo", "sha123", "refs/heads/main", ("always",), 60)
        self.assertEqual(resolve_workflow_job(self.repo, other)[1].id, "always")

    def test_workflow_to_contract_rejects_step_if_condition(self) -> None:
        workflow_file = self.workflows_dir / "step-if.yml"
        workflow_file.write_text(
            """
name: Step If
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo 1
        if: always()
"""
        )
        wf = parse_workflow(workflow_file)
        with self.assertRaises(WorkflowError) as cm:
            workflow_to_contract(wf.jobs["test"], self.context())
        self.assertIn("'if' condition", str(cm.exception))
        self.assertIn("requires act engine", str(cm.exception))

    def test_workflow_to_contract_translation(self) -> None:
        workflow_file = self.workflows_dir / "build.yaml"
        workflow_file.write_text(
            """
name: Build
on: [push]
jobs:
  build:
    runs-on: ubuntu-22.04
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npm run build
"""
        )
        wf = parse_workflow(workflow_file)
        contract = workflow_to_contract(wf.jobs["build"], self.context())
        self.assertEqual(contract.image, "ubuntu:22.04")
        self.assertEqual(contract.steps, ("npm ci", "npm run build"))

    def test_workflow_to_contract_rejects_unsupported_action(self) -> None:
        workflow_file = self.workflows_dir / "unsupported.yml"
        workflow_file.write_text(
            """
name: Custom Action
on: [push]
jobs:
  custom:
    runs-on: ubuntu-latest
    steps:
      - uses: docker/setup-buildx-action@v4
      - run: npm test
"""
        )
        wf = parse_workflow(workflow_file)
        with self.assertRaises(WorkflowError) as cm:
            workflow_to_contract(wf.jobs["custom"], self.context())
        self.assertIn("unsupported action 'docker/setup-buildx-action@v4'", str(cm.exception))
        self.assertIn("step 1", str(cm.exception))
        self.assertIn("requires act engine", str(cm.exception))

    def test_resolves_workflow_contract_for_matching_event(self) -> None:
        (self.workflows_dir / "ci.yml").write_text(
            """
name: CI
on:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: make test
"""
        )
        job = JobSpec("job1", "org/repo", "sha123", "refs/heads/main", (), 60)
        contract = resolve_workflow_contract(self.repo, job)
        self.assertEqual(contract.image, "ubuntu:24.04")
        self.assertEqual(contract.steps, ("make test",))

    def test_fails_when_no_workflows_or_contract_exist(self) -> None:
        empty_repo = self.repo / "empty"
        empty_repo.mkdir()
        job = JobSpec("job1", "org/repo", "sha123", "refs/heads/main", (), 60)
        with self.assertRaises(WorkflowError):
            resolve_workflow_contract(empty_repo, job)

    def test_ambiguous_target_job_raises_error(self) -> None:
        (self.workflows_dir / "first.yml").write_text(
            """
name: First
on: [push]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: echo 1
"""
        )
        (self.workflows_dir / "second.yml").write_text(
            """
name: Second
on: [push]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: echo 2
"""
        )
        job = JobSpec("job1", "org/repo", "sha123", "refs/heads/main", ("lint",), 60)
        with self.assertRaises(WorkflowError) as cm:
            resolve_workflow_job(self.repo, job)
        self.assertIn("multiple workflow jobs matched", str(cm.exception))

    def test_create_event_payload_synthesizes_push_and_pull_request_events(self) -> None:
        push_job = JobSpec("job1", "my-org/my-app", "abc1234", "refs/heads/main", (), 60)
        push_payload = create_event_payload(push_job, "push")
        self.assertEqual(push_payload["repository"]["full_name"], "my-org/my-app")
        self.assertEqual(push_payload["ref"], "refs/heads/main")
        self.assertEqual(push_payload["after"], "abc1234")
        self.assertNotIn("pull_request", push_payload)

        pr_job = JobSpec("job2", "my-org/my-app", "def5678", "refs/pull/42/merge", (), 60)
        pr_payload = create_event_payload(pr_job, "pull_request")
        self.assertIn("pull_request", pr_payload)
        self.assertEqual(pr_payload["pull_request"]["number"], 42)
        # the queued record does not say who opened it, which branch it came
        # from, or which branch it merges into, so the payload says none of them
        # rather than a value no reader could trust
        for fabricated in ("action", "default_branch", "sender"):
            self.assertNotIn(fabricated, pr_payload)
        self.assertNotIn("base", pr_payload["pull_request"])

        pr_head_job = JobSpec("job3", "my-org/my-app", "def5678", "refs/pull/99/head", (), 60)
        pr_head_payload = create_event_payload(pr_head_job, "pull_request")
        self.assertEqual(pr_head_payload["pull_request"]["number"], 99)

    def test_workflow_to_contract_rejects_a_step_that_names_nothing(self) -> None:
        workflow_file = self.workflows_dir / "nameless.yml"
        workflow_file.write_text(
            """
name: Nameless
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo 1
      - name: nothing to do
"""
        )
        wf = parse_workflow(workflow_file)
        with self.assertRaises(WorkflowError) as cm:
            workflow_to_contract(wf.jobs["test"], self.context())
        self.assertIn("neither 'run' nor 'uses'", str(cm.exception))


class ActRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.workflows_dir = self.repo / ".github" / "workflows"
        self.workflows_dir.mkdir(parents=True)

    def write(self, name: str, body: str) -> None:
        (self.workflows_dir / name).write_text(body)

    def contract_for(self, job_id="job1", ref="refs/heads/main"):
        return resolve_workflow_contract(
            self.repo, JobSpec(job_id, "org/repo", "sha123", ref, (), 60)
        )

    def test_a_job_that_names_an_action_runs_through_the_act_engine(self) -> None:
        self.write(
            "ci.yml",
            """
name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-22.04
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
      - run: npm test
""",
        )
        contract = self.contract_for()
        self.assertEqual(contract.image, ACT_IMAGE)
        self.assertTrue(contract.docker)
        self.assertEqual(len(contract.steps), 1)
        step = contract.steps[0]
        self.assertIn(f"exec {ACT_BINARY} push", step)
        self.assertIn("-j build", step)
        self.assertIn("ubuntu-22.04=ubuntu:22.04", step)
        self.assertIn("actions/setup-node@v4", step)

    def test_a_step_condition_and_working_directory_also_reach_the_engine(self) -> None:
        self.write(
            "ci.yml",
            """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pnpm test
        working-directory: web
      - run: echo done
        if: success()
""",
        )
        step = self.contract_for().steps[0]
        self.assertIn("working-directory", step)
        self.assertIn("-P", step)
        self.assertIn("ubuntu-latest=ubuntu:24.04", step)

    def test_a_run_only_job_still_translates_natively(self) -> None:
        self.write(
            "ci.yml",
            """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: make test
""",
        )
        contract = self.contract_for()
        self.assertEqual(contract.image, "ubuntu:24.04")
        self.assertEqual(contract.steps, ("make test",))
        self.assertFalse(contract.docker)

    def test_every_runs_on_label_is_mapped_so_act_cannot_run_on_the_host(self) -> None:
        self.write(
            "ci.yml",
            """
name: CI
on: [push]
jobs:
  test:
    runs-on: [self-hosted, linux, ubuntu-24.04]
    steps:
      - uses: actions/setup-go@v5
""",
        )
        step = self.contract_for().steps[0]
        for label in ("self-hosted", "linux", "ubuntu-24.04"):
            self.assertIn(f"{label}=ubuntu:24.04", step)

    def test_a_pull_request_job_runs_with_a_pull_request_event(self) -> None:
        self.write(
            "ci.yml",
            """
name: CI
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@v4
""",
        )
        step = self.contract_for(ref="refs/pull/7/merge").steps[0]
        self.assertIn(f"exec {ACT_BINARY} pull_request", step)
        self.assertIn('"number": 7', step)


    def test_a_matrix_job_is_refused_rather_than_run_once(self) -> None:
        self.write(
            "ci.yml",
            """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        node: [20, 22]
    steps:
      - uses: actions/setup-node@v4
""",
        )
        with self.assertRaises(WorkflowError) as cm:
            self.contract_for()
        self.assertIn("matrix", str(cm.exception))

    def test_a_job_that_needs_another_job_is_refused(self) -> None:
        self.write(
            "ci.yml",
            """
name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: make build
  test:
    needs: [build]
    runs-on: ubuntu-latest
    steps:
      - run: make test
""",
        )
        with self.assertRaises(WorkflowError) as cm:
            resolve_workflow_contract(
                self.repo,
                JobSpec("job1", "org/repo", "sha123", "refs/heads/main", ("test",), 60),
            )
        self.assertIn("needs", str(cm.exception))

    def test_a_job_that_reads_a_secret_is_refused(self) -> None:
        self.write(
            "ci.yml",
            """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    secrets: NPM_TOKEN
    steps:
      - run: npm publish
        env:
          NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
""",
        )
        with self.assertRaises(WorkflowError) as cm:
            self.contract_for()
        self.assertIn("secrets", str(cm.exception))

    def test_an_unmapped_runs_on_is_refused_for_both_paths(self) -> None:
        self.write(
            "ci.yml",
            """
name: CI
on: [push]
jobs:
  test:
    runs-on: windows-latest
    steps:
      - uses: actions/setup-python@v5
""",
        )
        with self.assertRaises(WorkflowError) as cm:
            self.contract_for()
        self.assertIn("the device pool runs linux/amd64", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

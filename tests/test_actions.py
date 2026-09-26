import tempfile
import unittest
from pathlib import Path

from cindral.actions import (
    WorkflowError,
    build_act_command,
    create_event_payload,
    event_matches,
    find_workflows,
    parse_workflow,
    resolve_workflow_contract,
    resolve_workflow_job,
    workflow_to_contract,
)
from cindral.agent import JobSpec


class ActionsWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.workflows_dir = self.repo / ".github" / "workflows"
        self.workflows_dir.mkdir(parents=True)

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
            "pull_request": {"branches": ["main"]},
            "workflow_dispatch": None,
        }
        self.assertTrue(event_matches(on_spec, "push", "refs/heads/main"))
        self.assertTrue(event_matches(on_spec, "push", "refs/heads/feature/auth"))
        self.assertFalse(event_matches(on_spec, "push", "refs/heads/bugfix/123"))
        self.assertFalse(event_matches(on_spec, "push", "refs/heads/other/merge"))
        self.assertTrue(event_matches(on_spec, "pull_request", "refs/pull/42/merge"))
        self.assertTrue(event_matches(on_spec, "workflow_dispatch", "refs/heads/main"))
        self.assertFalse(event_matches(on_spec, "schedule", "refs/heads/main"))

    def test_tag_filtering_and_tag_only_workflows(self) -> None:
        tag_spec = {"push": {"tags": ["v*"]}}
        self.assertTrue(event_matches(tag_spec, "push", "refs/tags/v1.0.0"))
        self.assertFalse(event_matches(tag_spec, "push", "refs/tags/release-1.0"))
        self.assertFalse(event_matches(tag_spec, "push", "refs/heads/main"))

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
            workflow_to_contract(wf.jobs["win"])
        self.assertIn("unsupported runs-on", str(cm.exception))

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
        if: failure()
"""
        )
        wf = parse_workflow(workflow_file)
        with self.assertRaises(WorkflowError) as cm:
            workflow_to_contract(wf.jobs["test"])
        self.assertIn("uses unsupported 'if' condition", str(cm.exception))

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
        contract = workflow_to_contract(wf.jobs["build"])
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
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: npm test
"""
        )
        wf = parse_workflow(workflow_file)
        with self.assertRaises(WorkflowError) as cm:
            workflow_to_contract(wf.jobs["custom"])
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

    def test_create_event_payload_synthesizes_push_and_pull_request_events(self) -> None:
        push_job = JobSpec("job1", "my-org/my-app", "abc1234", "refs/heads/main", (), 60)
        push_payload = create_event_payload(push_job, "push")
        self.assertEqual(push_payload["repository"]["full_name"], "my-org/my-app")
        self.assertEqual(push_payload["ref"], "refs/heads/main")
        self.assertNotIn("pull_request", push_payload)

        pr_job = JobSpec("job2", "my-org/my-app", "def5678", "refs/pull/42/merge", (), 60)
        pr_payload = create_event_payload(pr_job, "pull_request")
        self.assertEqual(pr_payload["action"], "opened")
        self.assertIn("pull_request", pr_payload)
        self.assertEqual(pr_payload["pull_request"]["number"], 42)

        pr_head_job = JobSpec("job3", "my-org/my-app", "def5678", "refs/pull/99/head", (), 60)
        pr_head_payload = create_event_payload(pr_head_job, "pull_request")
        self.assertEqual(pr_head_payload["pull_request"]["number"], 99)

    def test_build_act_command_constructs_flags_and_mappings(self) -> None:
        cmd = build_act_command(
            act="act",
            workflow_path=Path(".github/workflows/ci.yml"),
            job_id="test",
            event_name="push",
            event_path=Path("/tmp/event.json"),
            env={"CI": "true"},
        )
        self.assertEqual(cmd[:5], ["act", "push", "-W", ".github/workflows/ci.yml", "-j"])
        self.assertEqual(cmd[5], "test")
        self.assertIn("--eventpath", cmd)
        self.assertIn("/tmp/event.json", cmd)
        self.assertIn("--env", cmd)
        self.assertIn("CI=true", cmd)
        self.assertIn("-P", cmd)
        self.assertIn("ubuntu-latest=ubuntu:24.04", cmd)


if __name__ == "__main__":
    unittest.main()

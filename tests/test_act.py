"""Tests for the act engine path: the pinned engine, its argv, and the trust tier."""
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from cindral.act import (
    ACT_BINARY,
    ACT_IMAGE,
    ACT_VERSION,
    ActJob,
    CONTAINER_REPO,
    EVENT_PATH,
    act_contract,
    build_act_command,
    container_workflow_path,
    job_network,
)
from cindral.agent import JobSpec
from cindral.executor import DockerExecutor

ROOT = Path(__file__).resolve().parent.parent

WORKFLOW = """
name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    env:
      NODE_ENV: test
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: pnpm run ci
        working-directory: web
"""


def act_job(**overrides) -> ActJob:
    repo = Path("/var/lib/cindral/work/job-1/repo")
    request = {
        "workflow_path": repo / ".github" / "workflows" / "ci.yml",
        "repo": repo,
        "job_id": "build",
        "event_name": "push",
        "event": {"ref": "refs/heads/main", "sha": "a" * 40},
        "reason": "unsupported action 'actions/setup-node@v4'",
        "platforms": {"ubuntu-latest": "ubuntu:24.04"},
        "steps": ({"uses": "actions/setup-node@v4"}, {"run": "pnpm run ci"}),
        "env": {"NODE_ENV": "test"},
        "network": job_network("job-1"),
        "timeout_minutes": 15,
    }
    return ActJob(**{**request, **overrides})


def flags(argv: list[str], flag: str) -> list[str]:
    return [argv[index + 1] for index, part in enumerate(argv) if part == flag]


class BuildActCommandTest(unittest.TestCase):
    def test_command_pins_the_engine_and_the_checkout(self) -> None:
        argv = build_act_command(
            act=ACT_BINARY,
            workflow_path=f"{CONTAINER_REPO}/.github/workflows/ci.yml",
            workdir=CONTAINER_REPO,
            job_id="build",
            event_name="pull_request",
            event_path=EVENT_PATH,
            platforms={"ubuntu-latest": "ubuntu:24.04", "self-hosted": "ubuntu:24.04"},
            env={"B": "2", "A": "1"},
            network=job_network("job-1"),
        )
        self.assertEqual(
            argv[:8],
            [
                ACT_BINARY,
                "pull_request",
                "-W",
                f"{CONTAINER_REPO}/.github/workflows/ci.yml",
                "-C",
                CONTAINER_REPO,
                "-j",
                "build",
            ],
        )
        self.assertEqual(flags(argv, "--eventpath"), [EVENT_PATH])
        # binding the checkout would hand the step an empty workspace, because the
        # daemon resolves a bind source on the host
        self.assertIn("--bind=false", argv)
        # the engine keeps the socket to itself
        self.assertEqual(flags(argv, "--container-daemon-socket"), ["-"])
        self.assertEqual(flags(argv, "--network"), ["cindral-job-1"])
        self.assertEqual(
            flags(argv, "-P"),
            ["self-hosted=ubuntu:24.04", "ubuntu-latest=ubuntu:24.04"],
        )
        self.assertEqual(flags(argv, "--env"), ["A=1", "B=2"])

    def test_command_omits_the_optional_arguments(self) -> None:
        argv = build_act_command(
            act="act", workflow_path="w.yml", workdir=".", job_id="build", event_name="push"
        )
        self.assertEqual(
            argv,
            [
                "act",
                "push",
                "-W",
                "w.yml",
                "-C",
                ".",
                "-j",
                "build",
                "--bind=false",
                "--container-daemon-socket",
                "-",
            ],
        )


class ActContractTest(unittest.TestCase):
    def test_contract_runs_the_pinned_engine_in_the_socket_tier(self) -> None:
        contract = act_contract(act_job())
        self.assertEqual(contract.image, ACT_IMAGE)
        self.assertTrue(contract.docker)
        self.assertEqual(contract.timeout_minutes, 15)
        # the workflow env reaches the step containers through act, not the engine
        # container, where it could point the engine at another Docker daemon
        self.assertEqual(contract.env, {})
        self.assertEqual(len(contract.steps), 1)

    def test_script_runs_act_with_the_event_and_the_actions_it_downloads(self) -> None:
        request = act_job()
        step = act_contract(request).steps[0]
        self.assertIn(f"act {ACT_VERSION}", step)
        self.assertIn("runs job", step)
        self.assertIn("unsupported action", step)
        # third-party code act downloads and runs, named in the log before it runs
        self.assertIn("actions/setup-node@v4", step)
        self.assertIn(f"exec {ACT_BINARY} push", step)
        self.assertIn("ubuntu-latest=ubuntu:24.04", step)
        self.assertIn("NODE_ENV=test", step)
        self.assertIn("-j build", step)
        self.assertIn(f"> {EVENT_PATH}", step)
        self.assertIn(json.dumps(request.event, sort_keys=True), step)

    def test_script_quotes_the_event_so_a_payload_cannot_reach_the_shell(self) -> None:
        payload = {"ref": "refs/heads/x; rm -rf /"}
        step = act_contract(act_job(event=payload)).steps[0]
        self.assertIn(f"'{{\"ref\": \"refs/heads/x; rm -rf /\"}}' > {EVENT_PATH}", step)

    def test_container_workflow_path_follows_the_checkout(self) -> None:
        repo = Path("/srv/checkout")
        self.assertEqual(
            container_workflow_path(repo / ".github" / "workflows" / "ci.yml", repo),
            f"{CONTAINER_REPO}/.github/workflows/ci.yml",
        )
        with self.assertRaises(ValueError):
            container_workflow_path(Path("/elsewhere/ci.yml"), repo)


class ActPinTest(unittest.TestCase):
    def test_image_matches_the_pinned_release_in_the_dockerfile(self) -> None:
        dockerfile = (ROOT / "Dockerfile.act").read_text(encoding="utf-8")
        pinned = re.search(r"^ARG ACT_VERSION=(\S+)$", dockerfile, re.MULTILINE)
        self.assertIsNotNone(pinned, "Dockerfile.act no longer pins an act version")
        self.assertEqual(pinned.group(1), ACT_VERSION)
        self.assertEqual(ACT_IMAGE, f"cindral-act:{ACT_VERSION}")
        for architecture in ("AMD64", "ARM64"):
            checksum = re.search(
                rf"^ARG ACT_SHA256_{architecture}=([0-9a-f]{{64}})$", dockerfile, re.MULTILINE
            )
            self.assertIsNotNone(checksum, f"no pinned act checksum for {architecture}")

    def test_the_engine_image_is_built_by_its_own_dockerfile(self) -> None:
        # the main image is built with the legacy builder, which runs every stage
        # in order, so an act stage in the main Dockerfile would download and
        # verify act on every build of the broker and panel images
        main = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("ACT_VERSION", main, "the act pin moved into the main Dockerfile")
        self.assertIn("ACT_VERSION", (ROOT / "Dockerfile.act").read_text(encoding="utf-8"))

    def test_the_engine_image_does_not_depend_on_a_buildkit_only_variable(self) -> None:
        # TARGETARCH is set by BuildKit alone; the Cindral contract builds with
        # the legacy builder, where an unset value under `set -eu` fails the step
        dockerfile = (ROOT / "Dockerfile.act").read_text(encoding="utf-8")
        code = [line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")]
        body = "\n".join(code)
        self.assertNotIn("ARG TARGETARCH", body)
        self.assertNotIn("$TARGETARCH", body)
        self.assertIn("dpkg --print-architecture", body)


class RecordingRunner:
    """Records argv, materializes the checkout on `git init`, and reads run.sh."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.files = files or {}
        self.script = ""

    def run(self, argv, log, env=None, cwd=None, cancel=None):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if "git init" in joined:
            repo = Path(argv[-1])
            for name, value in self.files.items():
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
        if "-v" in argv:
            mount = Path(argv[argv.index("-v") + 1].rsplit(":", 1)[0])
            if (mount / "run.sh").exists():
                self.script = (mount / "run.sh").read_text()
        return 0

    def check(self, argv, log, env=None, cwd=None, cancel=None):
        if self.run(argv, log, env=env, cwd=cwd, cancel=cancel) != 0:
            raise RuntimeError(f"command failed: {argv}")

    def capture(self, argv):
        self.calls.append(list(argv))
        return 0, "healthy\n"


def spec(job_id="job-1"):
    return JobSpec(job_id, "example-org/example-app", "a" * 40, "refs/heads/main", (), 3600)


def act_executor(runner, workspace):
    """An executor whose device has a Docker socket, as an act job requires."""
    socket = Path(workspace) / "docker.sock"
    socket.touch()
    fake_docker = Path(workspace) / "docker"
    fake_docker.write_text("#!/bin/sh\n")
    fake_docker.chmod(0o755)
    executor = DockerExecutor(
        workspace_root=Path(workspace) / "work",
        log_dir=str(Path(workspace) / "logs"),
        runner=runner,
        docker=str(fake_docker),
    )
    patches = [
        mock.patch("cindral.docker_support.DOCKER_SOCKET", str(socket)),
        mock.patch("cindral.docker_support.DOCKER_PLUGIN_DIRS", ()),
    ]
    return executor, patches


class ActExecutionTest(unittest.TestCase):
    def run_job(self, runner, job=None):
        with tempfile.TemporaryDirectory() as tmp:
            executor, patches = act_executor(runner, tmp)
            with patches[0], patches[1]:
                return executor(job or spec(), threading.Event())

    def test_a_workflow_job_runs_in_the_engine_container_with_the_socket(self) -> None:
        runner = RecordingRunner({".github/workflows/ci.yml": WORKFLOW})
        result = self.run_job(runner)
        self.assertEqual(result.exit_code, 0, result.log)
        self.assertIn("cindral-act", runner.script)
        self.assertIn(f"exec {ACT_BINARY} push", runner.script)
        self.assertIn("-j build", runner.script)
        job_call = next(call for call in runner.calls if "run.sh" in " ".join(call))
        self.assertIn(ACT_IMAGE, job_call)
        self.assertIn("/workspace/repo", job_call)
        self.assertTrue(any("-v" in call and "docker.sock:" in " ".join(call) for call in runner.calls))

    def test_the_engine_joins_the_network_the_executor_creates(self) -> None:
        runner = RecordingRunner({".github/workflows/ci.yml": WORKFLOW})
        result = self.run_job(runner)
        self.assertEqual(result.exit_code, 0, result.log)
        created = [
            call[call.index("create") + 1]
            for call in runner.calls
            if "network" in call and "create" in call
        ]
        self.assertEqual(created, [job_network("job-1")])
        self.assertIn(f"--network {job_network('job-1')}", runner.script)

    def test_a_device_without_a_socket_fails_before_any_step(self) -> None:
        runner = RecordingRunner({".github/workflows/ci.yml": WORKFLOW})
        with tempfile.TemporaryDirectory() as tmp:
            executor = DockerExecutor(
                workspace_root=tmp,
                log_dir=str(Path(tmp) / "logs"),
                runner=runner,
                docker=str(Path(tmp) / "docker"),
            )
            with mock.patch(
                "cindral.docker_support.DOCKER_SOCKET", str(Path(tmp) / "missing.sock")
            ):
                result = executor(spec(), threading.Event())
        self.assertEqual(result.exit_code, 1)
        self.assertIn("docker but", result.log)
        self.assertEqual(runner.script, "")


if __name__ == "__main__":
    unittest.main()

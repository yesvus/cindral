import hashlib
import platform
import shutil
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from cindral.agent import JobSpec
from cindral.contract import CONTRACT_PATH
from cindral.cache_adapters import architecture_name
from cindral.executor import DockerExecutor, ProcessRunner, tail

CONTRACT = """
[image]
ref = "node:22-bookworm"

[[steps]]
run = "pnpm install --frozen-lockfile"

[[steps]]
run = "pnpm run ci"
"""

DOCKER_CONTRACT = """
[image]
ref = "node:22-bookworm"

[runner]
docker = true

[[steps]]
run = "docker build -t app:ci ."
"""

HEALTH_SERVICE = """
[[services]]
name = "postgres"
image = "pgvector/pgvector:pg16"
health_cmd = "pg_isready -U postgres"
"""


def spec(job_id="job-1", sha="a" * 40):
    return JobSpec(
        id=job_id,
        repository="example-org/example-app",
        sha=sha,
        ref="main",
        command=(),
        timeout=3600,
    )


class FakeRunner:
    """Records argv, materializes the checkout on `git init`, and reads run.sh."""

    def __init__(self, contract=CONTRACT, codes=None, cancel_on=None, health="healthy", files=None):
        self.calls = []
        self.contract = contract
        self.codes = codes or {}
        self.cancel_on = cancel_on
        self.health = health
        self.files = files or {}
        self.cache_was_present = False
        self.script = None

    def _workspace(self, argv):
        if "-v" in argv:
            mount = argv[argv.index("-v") + 1]
            return Path(mount.rsplit(":", 1)[0])
        return None

    def run(self, argv, log, env=None, cwd=None, cancel=None):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        workspace = self._workspace(argv)
        if workspace is not None and (workspace / "run.sh").exists():
            self.script = (workspace / "run.sh").read_text()
            joined += " " + self.script
        if "git init" in " ".join(argv):
            repo = Path(argv[-1])
            if self.contract is not None:
                (repo / ".cindral").mkdir(parents=True, exist_ok=True)
                (repo / CONTRACT_PATH).write_text(self.contract)
            for name, value in self.files.items():
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
        if "run.sh" in joined and "/cindral-cache" in joined:
            cache_mount = next(
                value
                for index, value in enumerate(argv)
                if index and argv[index - 1] == "-v" and value.endswith(":/cindral-cache")
            )
            cache_root = Path(cache_mount.rsplit(":", 1)[0])
            store = cache_root / "pnpm"
            self.cache_was_present = (store / "package.data").is_file()
            store.mkdir(parents=True, exist_ok=True)
            (store / "package.data").write_text("cached package")
        if self.cancel_on and self.cancel_on in joined and cancel is not None:
            cancel.set()
        for marker, code in self.codes.items():
            if marker in joined:
                log.write(f"fake: {marker} -> {code}\n")
                return code
        return 0

    def check(self, argv, log, env=None, cwd=None, cancel=None):
        code = self.run(argv, log, env=env, cwd=cwd, cancel=cancel)
        if code != 0:
            raise RuntimeError(f"command failed: {argv}")

    def capture(self, argv):
        self.calls.append(list(argv))
        return 0, f"{self.health}\n"


def executor(runner, workspace, docker="docker"):
    return DockerExecutor(
        token="tok",
        workspace_root=workspace,
        log_dir=str(Path(workspace) / "logs"),
        runner=runner,
        docker=docker,
    )


class FakeCacheClient:
    def __init__(self):
        self.entries = {}
        self.blobs = {}
        self.lookups = []
        self.uploads = []

    def lookup(self, repository, branch, architecture, key, restore_keys=()):
        self.lookups.append((repository, branch, architecture, key, restore_keys))
        return self.entries.get((repository, branch, architecture, key))

    def download(self, digest, destination, repository, branch, architecture, key):
        destination.write_bytes(self.blobs[digest])

    def upload(self, repository, branch, architecture, key, source):
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        scope = (repository, branch, architecture, key)
        self.uploads.append(scope)
        if scope in self.entries:
            return {"created": False, "entry": self.entries[scope]}
        entry = {"repository": repository, "branch": branch, "architecture": architecture, "key": key, "digest": digest}
        self.entries[scope] = entry
        self.blobs[digest] = source.read_bytes()
        return {"created": True, "entry": entry}


class DockerExecutorTest(unittest.TestCase):
    def test_runs_the_contract_in_one_container(self) -> None:
        runner = FakeRunner()
        with TemporaryDirectory() as tmp:
            result = executor(runner, tmp)(spec(), threading.Event())
        self.assertEqual(result.exit_code, 0)
        self.assertIn("set -e", runner.script)
        self.assertIn("'pnpm install --frozen-lockfile'", runner.script)
        self.assertIn("'pnpm run ci'", runner.script)
        joined = [" ".join(call) for call in runner.calls]
        self.assertTrue(any("docker network create cindral-job-1" in c for c in joined))
        self.assertTrue(any("docker network rm cindral-job-1" in c for c in joined))
        self.assertTrue(any("run.sh" in c for c in joined))
        self.assertTrue(any("checkout --detach" in c for c in joined))

    def test_a_failing_script_returns_its_code(self) -> None:
        runner = FakeRunner(codes={"pnpm install": 3})
        with TemporaryDirectory() as tmp:
            result = executor(runner, tmp)(spec(), threading.Event())
        self.assertEqual(result.exit_code, 3)

    def test_restores_and_stores_a_pnpm_cache_using_lockfile_and_architecture(self) -> None:
        runner = FakeRunner(files={"pnpm-lock.yaml": "lockfileVersion: '9.0'\n"})
        cache = FakeCacheClient()
        with TemporaryDirectory() as tmp:
            result = DockerExecutor(
                workspace_root=tmp,
                log_dir=str(Path(tmp) / "logs"),
                runner=runner,
                cache_client=cache,
            )(spec(), threading.Event())

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(cache.lookups), 1)
        self.assertEqual(
            cache.lookups[0][:3],
            ("example-org/example-app", "main", architecture_name(platform.machine())),
        )
        self.assertEqual(len(cache.uploads), 1)
        self.assertIn("cache miss pnpm", result.log)
        self.assertIn("cache stored pnpm", result.log)
        job_call = next(call for call in runner.calls if "run.sh" in " ".join(call))
        self.assertIn("npm_config_store_dir=/cindral-cache/pnpm", job_call)
        self.assertTrue(any(value.endswith(":/cindral-cache") for value in job_call))

        second_runner = FakeRunner(files={"pnpm-lock.yaml": "lockfileVersion: '9.0'\n"})
        with TemporaryDirectory() as tmp:
            second_result = DockerExecutor(
                workspace_root=tmp,
                log_dir=str(Path(tmp) / "logs"),
                runner=second_runner,
                cache_client=cache,
            )(spec(job_id="job-2"), threading.Event())
        self.assertEqual(second_result.exit_code, 0)
        self.assertTrue(second_runner.cache_was_present)
        self.assertIn("cache hit pnpm", second_result.log)

    def test_does_not_store_a_cache_after_a_failed_job(self) -> None:
        runner = FakeRunner(codes={"pnpm run ci": 3}, files={"pnpm-lock.yaml": "lock"})
        cache = FakeCacheClient()
        with TemporaryDirectory() as tmp:
            result = DockerExecutor(workspace_root=tmp, runner=runner, cache_client=cache)(spec(), threading.Event())
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(cache.uploads, [])

    def test_retries_workspace_cleanup_in_a_root_container(self) -> None:
        runner = FakeRunner()
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "job-1"
            original_rmtree = shutil.rmtree
            failed = False

            def fail_once(path, *args, **kwargs):
                nonlocal failed
                if Path(path) == workspace and workspace.exists() and not failed:
                    failed = True
                    raise PermissionError("root-owned files")
                return original_rmtree(path, *args, **kwargs)

            with mock.patch("cindral.executor.shutil.rmtree", side_effect=fail_once):
                result = executor(runner, tmp)(spec(), threading.Event())

            self.assertEqual(result.exit_code, 0)
            self.assertFalse(workspace.exists())

        cleanup_calls = [call for call in runner.calls if "--user" in call]
        self.assertEqual(len(cleanup_calls), 1)
        self.assertIn("0:0", cleanup_calls[0])
        self.assertIn("node:22-bookworm", cleanup_calls[0])

    def test_reports_workspace_cleanup_failure(self) -> None:
        runner = FakeRunner(codes={"--user 0:0": 1})
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "job-1"
            original_rmtree = shutil.rmtree
            failed = False

            def fail_once(path, *args, **kwargs):
                nonlocal failed
                if Path(path) == workspace and workspace.exists() and not failed:
                    failed = True
                    raise PermissionError("root-owned files")
                return original_rmtree(path, *args, **kwargs)

            with mock.patch("cindral.executor.shutil.rmtree", side_effect=fail_once):
                result = executor(runner, tmp)(spec(), threading.Event())

            self.assertEqual(result.exit_code, 0)
            self.assertIn("warning: could not remove root-owned files", result.log)

    def test_starts_and_stops_declared_services(self) -> None:
        contract = CONTRACT.replace(
            '[image]',
            '[[services]]\nname = "postgres"\nimage = "pgvector/pgvector:pg16"\n\n[image]',
        )
        runner = FakeRunner(contract=contract)
        with TemporaryDirectory() as tmp:
            result = executor(runner, tmp)(spec(), threading.Event())
        joined = [" ".join(call) for call in runner.calls]
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(any("docker run -d --name cindral-job-1-postgres" in c for c in joined))
        self.assertTrue(any("docker rm -f cindral-job-1-postgres" in c for c in joined))

    def test_mounts_the_docker_socket_for_docker_contracts(self) -> None:
        runner = FakeRunner(contract=DOCKER_CONTRACT)
        with TemporaryDirectory() as tmp:
            socket = Path(tmp) / "docker.sock"
            socket.touch()
            fake = Path(tmp) / "docker"
            fake.write_text("#!/bin/sh\n")
            fake.chmod(0o755)
            with mock.patch("cindral.docker_support.DOCKER_SOCKET", str(socket)), mock.patch(
                "cindral.docker_support.DOCKER_PLUGIN_DIRS", ()
            ):
                result = executor(runner, tmp, docker=str(fake))(spec(), threading.Event())
        joined = [" ".join(call) for call in runner.calls]
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(any(f"-v {socket}:{socket}" in c for c in joined))
        self.assertTrue(any(f"-v {fake}:{fake}:ro" in c for c in joined))
        self.assertTrue(any("--group-add" in c for c in joined))

    def test_missing_docker_socket_is_reported_as_executor_failure(self) -> None:
        runner = FakeRunner(contract=DOCKER_CONTRACT)
        with TemporaryDirectory() as tmp:
            with mock.patch("cindral.docker_support.DOCKER_SOCKET", str(Path(tmp) / "missing.sock")):
                result = executor(runner, tmp)(spec(), threading.Event())
        self.assertEqual(result.exit_code, 1)
        self.assertIn("docker but", result.log)

    def test_waits_for_a_service_health_check(self) -> None:
        contract = CONTRACT.replace("[image]", HEALTH_SERVICE + "\n[image]")
        runner = FakeRunner(contract=contract)
        with TemporaryDirectory() as tmp:
            result = executor(runner, tmp)(spec(), threading.Event())
        joined = [" ".join(call) for call in runner.calls]
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(any("--health-cmd pg_isready -U postgres" in c for c in joined))
        self.assertTrue(any("State.Health.Status" in c for c in joined))
        self.assertIn("waiting for postgres to become healthy", result.log)

    def test_an_unhealthy_service_fails_the_job(self) -> None:
        contract = CONTRACT.replace("[image]", HEALTH_SERVICE + "\n[image]")
        runner = FakeRunner(contract=contract, health="starting")
        with TemporaryDirectory() as tmp, mock.patch("cindral.executor.SERVICE_HEALTH_TIMEOUT", 0):
            result = executor(runner, tmp)(spec(), threading.Event())
        self.assertEqual(result.exit_code, 1)
        self.assertIn("did not become healthy", result.log)

    def test_missing_contract_fails_the_job(self) -> None:
        runner = FakeRunner(contract=None)
        with TemporaryDirectory() as tmp:
            result = executor(runner, tmp)(spec(), threading.Event())
        self.assertEqual(result.exit_code, 1)
        self.assertIn("no .cindral/ci.toml", result.log)

    def test_cancel_returns_130(self) -> None:
        runner = FakeRunner(cancel_on="pnpm install")
        with TemporaryDirectory() as tmp:
            result = executor(runner, tmp)(spec(), threading.Event())
        self.assertEqual(result.exit_code, 130)

    def test_tail_keeps_the_end_of_long_logs(self) -> None:
        text = "x" * 100 + "END"
        self.assertTrue(tail(text, limit=10).endswith("END"))
        self.assertNotIn("x" * 100, tail(text, limit=10))

    def test_executes_job_from_github_actions_workflow(self) -> None:
        workflow_yaml = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - run: npm test
"""
        runner = FakeRunner(contract=None, files={".github/workflows/ci.yml": workflow_yaml})
        with TemporaryDirectory() as tmp:
            result = executor(runner, tmp)(spec(), threading.Event())
        self.assertEqual(result.exit_code, 0)
        self.assertIn("npm test", runner.script)


class ProcessRunnerTest(unittest.TestCase):
    def test_missing_binary_returns_127(self) -> None:
        import io

        runner = ProcessRunner()
        with TemporaryDirectory() as tmp:
            code = runner.run(["definitely-not-a-binary-xyz"], io.StringIO(), cwd=tmp)
        self.assertEqual(code, 127)


if __name__ == "__main__":
    unittest.main()

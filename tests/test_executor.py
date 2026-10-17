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
from cindral.executor import MIN_JOB_CPUS, DockerExecutor, ExecutorError, ProcessRunner, tail

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

DOCKERFILE_CONTRACT = """
[image]
dockerfile = ".cindral/Dockerfile"

[[steps]]
run = "pnpm run ci"
"""

# `docker build --help` excerpts the executor probes for limit flags
BUILDKIT_HELP = "      --resource stringArray          Resource limits for build\n"
LEGACY_HELP = "      --cpu-quota int                 Limit CPU CFS quota\n"
NO_LIMIT_HELP = "Usage:  docker build [OPTIONS] PATH | URL | -\n"


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

    def __init__(self, contract=CONTRACT, codes=None, cancel_on=None, health="healthy", files=None, build_help=BUILDKIT_HELP, contracts_by_job=None):
        self.calls = []
        self.contract = contract
        self.codes = codes or {}
        self.cancel_on = cancel_on
        self.health = health
        self.files = files or {}
        self.build_help = build_help
        self.contracts_by_job = contracts_by_job or {}
        self.cache_was_present = False
        self.script = None
        self.builds = 0
        self.lock = threading.Lock()

    def _workspace(self, argv):
        if "-v" in argv:
            mount = argv[argv.index("-v") + 1]
            return Path(mount.rsplit(":", 1)[0])
        return None

    def run(self, argv, log, env=None, cwd=None, cancel=None):
        with self.lock:
            self.calls.append(list(argv))
        joined = " ".join(argv)
        if "build" in argv:
            self.builds += 1
        workspace = self._workspace(argv)
        if workspace is not None and (workspace / "run.sh").exists():
            self.script = (workspace / "run.sh").read_text()
            joined += " " + self.script
        if "git init" in " ".join(argv):
            repo = Path(argv[-1])
            contract = self.contract
            if self.contracts_by_job:
                contract = next(
                    value for name, value in self.contracts_by_job.items() if name in repo.parts
                )
            if contract is not None:
                (repo / ".cindral").mkdir(parents=True, exist_ok=True)
                (repo / CONTRACT_PATH).write_text(contract)
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
        if "build" in argv and "--help" in argv:
            return 0, self.build_help
        return 0, f"{self.health}\n"


def executor(runner, workspace, docker="docker", **kwargs):
    return DockerExecutor(
        token="tok",
        workspace_root=workspace,
        log_dir=str(Path(workspace) / "logs"),
        runner=runner,
        docker=docker,
        **kwargs,
    )


def build_argv(runner):
    """The real build invocation, not the `docker build --help` capability probe."""
    return next(call for call in runner.calls if "build" in call and "--help" not in call)


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

    def test_the_job_container_is_removed_when_the_run_ends(self) -> None:
        """Killing the `docker run` client leaves its container running on the device."""
        for cancel_on in (None, "pnpm install"):
            with self.subTest(cancel_on=cancel_on):
                runner = FakeRunner(cancel_on=cancel_on)
                with TemporaryDirectory() as tmp:
                    result = executor(runner, tmp)(spec(), threading.Event())
                joined = [" ".join(call) for call in runner.calls]
                self.assertIn("docker rm -f cindral-job-1-job", joined)
                self.assertIn("docker network rm cindral-job-1", joined)
                # the executor owns the removal, so the daemon is never left to
                # remove a container that is still running
                job_call = next(call for call in runner.calls if "run.sh" in " ".join(call))
                self.assertNotIn("--rm", job_call)

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
        self.assertIn("no CI definition found", result.log)

    def test_cancel_returns_130(self) -> None:
        runner = FakeRunner(cancel_on="pnpm install")
        with TemporaryDirectory() as tmp:
            result = executor(runner, tmp)(spec(), threading.Event())
        self.assertEqual(result.exit_code, 130)

    def test_tail_keeps_the_end_of_long_logs(self) -> None:
        text = "x" * 100 + "END"
        self.assertTrue(tail(text, limit=10).endswith("END"))
        self.assertNotIn("x" * 100, tail(text, limit=10))

    def test_job_and_service_containers_carry_the_configured_caps(self) -> None:
        contract = CONTRACT.replace(
            '[image]',
            '[[services]]\nname = "postgres"\nimage = "pgvector/pgvector:pg16"\n\n[image]',
        )
        runner = FakeRunner(contract=contract)
        with TemporaryDirectory() as tmp:
            result = executor(runner, tmp, cpus=2, memory="12g")(spec(), threading.Event())
        self.assertEqual(result.exit_code, 0)
        expected = (("--cpu-period", "100000"), ("--cpu-quota", "200000"), ("--memory", "12g"), ("--memory-swap", "12g"))
        job_call = next(call for call in runner.calls if "run.sh" in " ".join(call))
        service_call = next(call for call in runner.calls if "-d" in call and "postgres" in call)
        for call in (job_call, service_call):
            for flag, value in expected:
                self.assertEqual(call[call.index(flag) + 1], value)
        self.assertIn("caps --cpu-period 100000 --cpu-quota 200000 --memory 12g", result.log)

    def test_no_cap_flags_reach_a_job_without_configured_limits(self) -> None:
        runner = FakeRunner()
        with TemporaryDirectory() as tmp:
            result = executor(runner, tmp)(spec(), threading.Event())
        self.assertEqual(result.exit_code, 0)
        job_call = next(call for call in runner.calls if "run.sh" in " ".join(call))
        for flag in ("--cpu-quota", "--memory", "--memory-swap", "--cpus"):
            self.assertNotIn(flag, job_call)
        self.assertNotIn("caps", result.log)

    def test_a_cpu_cap_too_small_to_run_anything_is_refused(self) -> None:
        """A quota this small rounds to no usable cpu and the container would stall."""
        self.assertEqual(MIN_JOB_CPUS, 0.01)
        for cpus in (0.00001, 0.001, 0.009):
            with self.subTest(cpus=cpus), self.assertRaises(ExecutorError):
                DockerExecutor(cpus=cpus)
        args = DockerExecutor(cpus=MIN_JOB_CPUS).limits.run_args()
        self.assertEqual(args[args.index("--cpu-quota") + 1], "1000")

    def test_a_fractional_cpu_cap_becomes_a_period_quota(self) -> None:
        runner = FakeRunner()
        with TemporaryDirectory() as tmp:
            executor(runner, tmp, cpus=0.5)(spec(), threading.Event())
        job_call = next(call for call in runner.calls if "run.sh" in " ".join(call))
        self.assertEqual(job_call[job_call.index("--cpu-quota") + 1], "50000")

    def test_the_workspace_cleanup_container_is_capped(self) -> None:
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
                executor(runner, tmp, cpus=1, memory="2g")(spec(), threading.Event())
        cleanup_call = next(call for call in runner.calls if "--user" in call)
        self.assertEqual(cleanup_call[cleanup_call.index("--memory") + 1], "2g")

    def test_a_buildx_image_build_is_capped_with_resource_pairs(self) -> None:
        runner = FakeRunner(contract=DOCKERFILE_CONTRACT, files={".cindral/Dockerfile": "FROM node:22\n"})
        with TemporaryDirectory() as tmp:
            result = executor(runner, tmp, cpus=2, memory="12g")(spec(), threading.Event())
        self.assertEqual(result.exit_code, 0)
        build_call = build_argv(runner)
        self.assertIn("--resource", build_call)
        self.assertIn("cpu-quota=200000", build_call)
        self.assertIn("memory=12g", build_call)

    def test_a_legacy_builder_image_build_uses_the_run_flags(self) -> None:
        runner = FakeRunner(
            contract=DOCKERFILE_CONTRACT,
            files={".cindral/Dockerfile": "FROM node:22\n"},
            build_help=LEGACY_HELP,
        )
        with TemporaryDirectory() as tmp:
            result = executor(runner, tmp, cpus=2, memory="12g")(spec(), threading.Event())
        self.assertEqual(result.exit_code, 0)
        build_call = build_argv(runner)
        self.assertNotIn("--resource", build_call)
        self.assertEqual(build_call[build_call.index("--memory") + 1], "12g")

    def test_a_builder_without_limit_flags_leaves_the_build_uncapped_and_says_so(self) -> None:
        runner = FakeRunner(
            contract=DOCKERFILE_CONTRACT,
            files={".cindral/Dockerfile": "FROM node:22\n"},
            build_help=NO_LIMIT_HELP,
        )
        with TemporaryDirectory() as tmp:
            result = executor(runner, tmp, cpus=2, memory="12g")(spec(), threading.Event())
        self.assertEqual(result.exit_code, 0)
        build_call = build_argv(runner)
        for flag in ("--resource", "--memory", "--cpu-quota"):
            self.assertNotIn(flag, build_call)
        self.assertIn("the build is uncapped", result.log)

    def test_every_uncapped_build_says_so_although_the_probe_is_cached(self) -> None:
        """A cached capability answer must not silence the warning on later jobs."""
        runner = FakeRunner(
            contract=DOCKERFILE_CONTRACT,
            files={".cindral/Dockerfile": "FROM node:22\n"},
            build_help=NO_LIMIT_HELP,
        )
        with TemporaryDirectory() as tmp:
            shared = executor(runner, tmp, cpus=2, memory="12g")
            logs = [
                shared(spec(job_id=f"job-{index}"), threading.Event()).log
                for index in range(3)
            ]
        for log in logs:
            self.assertIn("the build is uncapped", log)
        # the probe still runs once; only the message repeats
        self.assertEqual(len([c for c in runner.calls if "build" in c and "--help" in c]), 1)

    def test_the_build_capability_probe_runs_once_per_executor(self) -> None:
        runner = FakeRunner(contract=DOCKERFILE_CONTRACT, files={".cindral/Dockerfile": "FROM node:22\n"})
        with TemporaryDirectory() as tmp:
            shared = executor(runner, tmp, cpus=2, memory="12g")
            for index in range(3):
                shared(spec(job_id=f"job-{index}"), threading.Event())
        probes = [call for call in runner.calls if "build" in call and "--help" in call]
        self.assertEqual(len(probes), 1)
        self.assertEqual(runner.builds, 3)

    def test_a_failure_after_the_image_still_gets_a_cleanup_container(self) -> None:
        """Dropping the image on the way out leaves root-owned files on the device."""
        runner = FakeRunner(contract=DOCKERFILE_CONTRACT, files={".cindral/Dockerfile": "FROM node:22\n"}, codes={"network create": 1})
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
                result = executor(runner, tmp, cpus=2, memory="12g")(spec(), threading.Event())

        self.assertEqual(result.exit_code, 1)
        cleanup_call = next(call for call in runner.calls if "--user" in call)
        self.assertEqual(cleanup_call[cleanup_call.index("--entrypoint") + 2], "cindral-job-job-1")

    def test_concurrent_jobs_keep_their_own_image_for_workspace_cleanup(self) -> None:
        """The cleanup image came from instance state, which one job overwrote."""
        runner = FakeRunner(
            contracts_by_job={
                "job-1": CONTRACT,
                "job-2": CONTRACT.replace("node:22-bookworm", "python:3.12-bookworm"),
            }
        )
        with TemporaryDirectory() as tmp:
            workspace_root = Path(tmp)
            shared = executor(runner, str(workspace_root))
            original_rmtree = shutil.rmtree
            blocked = {workspace_root / "job-1", workspace_root / "job-2"}
            failed: set = set()

            def fail_once(path, *args, **kwargs):
                target = Path(path)
                if target in blocked and target.exists() and target not in failed:
                    failed.add(target)
                    raise PermissionError("root-owned files")
                return original_rmtree(path, *args, **kwargs)

            with mock.patch("cindral.executor.shutil.rmtree", side_effect=fail_once):
                workers = [
                    threading.Thread(target=shared, args=(spec(job_id=name), threading.Event()))
                    for name in ("job-1", "job-2")
                ]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(20)
                    # a job that hung would leave its workspace and the run green
                    self.assertFalse(worker.is_alive(), "a concurrent job did not finish")

        cleanup_images = {
            call[call.index("-v") + 1].split(":")[0].rsplit("/", 1)[-1]: call[call.index("--entrypoint") + 2]
            for call in runner.calls
            if "--user" in call
        }
        self.assertEqual(len(cleanup_images), 2)
        self.assertEqual(cleanup_images["job-1"], "node:22-bookworm")
        self.assertEqual(cleanup_images["job-2"], "python:3.12-bookworm")

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

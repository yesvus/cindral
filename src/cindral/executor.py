"""On-device bounded-Docker executor for direct jobs.

Reads the repository contract (.cindral/ci.toml) from the checked-out commit,
runs the steps in a container on an isolated network with declared sidecars,
and returns the exit code plus captured output.
"""
import base64
import os
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import TextIO

from .agent import JobSpec
from .contract import CONTRACT_PATH, Contract, ContractError, Service
from .models import ExecutionResult

MAX_LOG_CHARS = 64 * 1024

DOCKER_SOCKET = "/var/run/docker.sock"
DOCKER_PLUGIN_DIRS = ("/usr/libexec/docker/cli-plugins", "/usr/local/lib/docker/cli-plugins")
SERVICE_HEALTH_TIMEOUT = 90
SERVICE_HEALTH_POLL_SECONDS = 2


class ExecutorError(RuntimeError):
    """Raised when the device cannot prepare or run a job."""


def tail(text: str, limit: int = MAX_LOG_CHARS) -> str:
    if len(text) <= limit:
        return text
    return "...(truncated)...\n" + text[-limit:]


class ProcessRunner:
    """Runs local processes, streaming their output into a log file."""

    def run(
        self,
        argv: list[str],
        log: TextIO,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        try:
            process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                env=env,
                cwd=cwd,
                bufsize=1,
            )
        except OSError as exc:
            log.write(f"cannot start {argv[0]}: {exc}\n")
            return 127
        stopped = threading.Event()

        def watch() -> None:
            while not stopped.wait(1):
                if cancel is not None and cancel.is_set():
                    process.kill()
                    return

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    log.write(line)
            process.wait()
        finally:
            stopped.set()
            if process.poll() is None:
                process.kill()
                process.wait()
        return process.returncode

    def check(
        self,
        argv: list[str],
        log: TextIO,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        code = self.run(argv, log, env=env, cwd=cwd, cancel=cancel)
        if code != 0:
            raise ExecutorError(f"command failed (exit {code}): {' '.join(argv)}")

    def capture(self, argv: list[str]) -> tuple[int, str]:
        """Runs a short command and returns its exit code and output."""
        try:
            completed = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
            )
        except OSError as exc:
            return 127, str(exc)
        return completed.returncode, completed.stdout or ""


class DockerExecutor:
    """Executor callable for the pull agent; runs a job in bounded Docker."""

    def __init__(
        self,
        token: str | None = None,
        workspace_root: str = "/var/lib/cindral/work",
        log_dir: str | None = None,
        docker: str = "docker",
        git: str = "git",
        github_base: str = "https://github.com",
        runner: ProcessRunner | None = None,
        shell: str = "sh",
        cleanup: bool = True,
    ) -> None:
        self.token = token or ""
        self.workspace_root = Path(workspace_root)
        self.log_dir = Path(log_dir) if log_dir else None
        self.docker = docker
        self.git = git
        self.github_base = github_base.rstrip("/")
        self.runner = runner or ProcessRunner()
        self.shell = shell
        self.cleanup = cleanup
        self._cleanup_image: str | None = None

    def __call__(self, job: JobSpec, cancel: threading.Event) -> ExecutionResult:
        workspace = self.workspace_root / job.id
        log_path = (self.log_dir / f"{job.id}.log") if self.log_dir else None
        self._cleanup_image = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle: TextIO = log_path.open("w")
        else:
            log_handle = open(os.devnull, "w")
        try:
            with log_handle as log:
                exit_code = self._execute(job, cancel, workspace, log)
        finally:
            if self.cleanup:
                cleanup_warning = self._cleanup_workspace(workspace)
            else:
                cleanup_warning = None
        text = log_path.read_text(errors="replace") if log_path else ""
        if cleanup_warning:
            if log_path is not None:
                with log_path.open("a") as log:
                    log.write(f"cindral: warning: {cleanup_warning}\n")
                text = log_path.read_text(errors="replace")
            else:
                text = f"cindral: warning: {cleanup_warning}\n"
        return ExecutionResult(exit_code=exit_code, log=tail(text))

    def _cleanup_workspace(self, workspace: Path) -> str | None:
        try:
            shutil.rmtree(workspace)
            return None
        except FileNotFoundError:
            return None
        except OSError as exc:
            if not workspace.exists():
                return None
            if not self._cleanup_image:
                return f"could not remove workspace {workspace}: {exc}"

        command = [
            self.docker,
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "-v",
            f"{workspace}:/cleanup",
            "--entrypoint",
            self.shell,
            self._cleanup_image,
            "-lc",
            "rm -rf /cleanup/* /cleanup/.[!.]* /cleanup/..?*",
        ]
        with open(os.devnull, "w") as log:
            code = self.runner.run(command, log)
        if code != 0:
            return f"could not remove root-owned files from workspace {workspace} (cleanup container exited {code})"
        try:
            shutil.rmtree(workspace)
        except FileNotFoundError:
            return None
        except OSError as exc:
            return f"could not remove workspace {workspace} after container cleanup: {exc}"
        return None

    def _execute(
        self,
        job: JobSpec,
        cancel: threading.Event,
        workspace: Path,
        log: TextIO,
    ) -> int:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir(parents=True)
        repo = workspace / "repo"
        try:
            log.write(f"cindral: job {job.id} {job.repository}@{job.sha[:12]}\n")
            self._checkout(job, repo, log, cancel)
            try:
                contract = Contract.parse((repo / CONTRACT_PATH).read_text())
            except FileNotFoundError as exc:
                raise ExecutorError(f"repository has no {CONTRACT_PATH}") from exc
            except ContractError as exc:
                raise ExecutorError(str(exc)) from exc
            image = self._image(job, repo, contract, log, cancel)
            self._cleanup_image = image
            return self._run_steps(job, contract, image, workspace, log, cancel)
        except ExecutorError as exc:
            log.write(f"cindral: {exc}\n")
            return 1
        except Exception as exc:  # noqa: BLE001 - report any device failure as a job failure
            log.write(f"cindral: unexpected executor failure: {exc!r}\n")
            return 1

    def _checkout(self, job: JobSpec, repo: Path, log: TextIO, cancel: threading.Event) -> None:
        url = f"{self.github_base}/{job.repository}.git"
        env = self._git_env()
        repo.mkdir(parents=True)
        self.runner.check([self.git, "init", "-q", str(repo)], log, cancel=cancel)
        self.runner.check([self.git, "-C", str(repo), "remote", "add", "origin", url], log, cancel=cancel)
        code = self.runner.run(
            [self.git, "-C", str(repo), "fetch", "--depth", "1", "origin", job.sha],
            log,
            env=env,
            cancel=cancel,
        )
        if code != 0:
            # fetching a bare sha is not always allowed; fall back to the ref
            self.runner.check([self.git, "-C", str(repo), "fetch", "origin", job.ref], log, env=env, cancel=cancel)
        self.runner.check([self.git, "-C", str(repo), "checkout", "--detach", job.sha], log, env=env, cancel=cancel)

    def _image(self, job: JobSpec, repo: Path, contract: Contract, log: TextIO, cancel: threading.Event) -> str:
        if contract.dockerfile:
            tag = f"cindral-job-{job.id}"
            dockerfile = repo / contract.dockerfile
            if not dockerfile.is_file():
                raise ExecutorError(f"dockerfile not found: {contract.dockerfile}")
            self.runner.check(
                [self.docker, "build", "-t", tag, "-f", str(dockerfile), str(repo)],
                log,
                cancel=cancel,
            )
            return tag
        assert contract.image is not None
        if self.runner.run([self.docker, "image", "inspect", contract.image], log, cancel=cancel) != 0:
            self.runner.check([self.docker, "pull", contract.image], log, cancel=cancel)
        return contract.image

    def _run_steps(
        self,
        job: JobSpec,
        contract: Contract,
        image: str,
        workspace: Path,
        log: TextIO,
        cancel: threading.Event,
    ) -> int:
        docker_cli = self._docker_cli(contract)
        network = f"cindral-{job.id}"
        # one container per job, so installs persist across steps the way they
        # do in a single CI job; the checkout is mounted from the workspace
        (workspace / "run.sh").write_text(self._script(contract.steps))
        services: list[str] = []
        unhealthy: list[tuple[str, Service]] = []
        self.runner.check([self.docker, "network", "create", network], log, cancel=cancel)
        try:
            for service in contract.services:
                name = f"{network}-{service.name}"
                argv = [self.docker, "run", "-d", "--name", name, "--network", network, "--network-alias", service.name]
                if service.health_cmd:
                    argv += [
                        "--health-cmd",
                        service.health_cmd,
                        "--health-interval",
                        "2s",
                        "--health-timeout",
                        "5s",
                        "--health-retries",
                        "30",
                    ]
                for key, value in service.env.items():
                    argv += ["-e", f"{key}={value}"]
                argv += [service.image, *service.command]
                self.runner.check(argv, log, cancel=cancel)
                services.append(name)
                if service.health_cmd:
                    unhealthy.append((name, service))
            if not self._wait_for_services(unhealthy, log, cancel):
                return 130
            argv = [
                self.docker,
                "run",
                "--rm",
                "--name",
                f"{network}-job",
                "--network",
                network,
                "-v",
                f"{workspace}:/workspace",
                "-w",
                "/workspace/repo",
                "-e",
                f"CINDRAL_JOB_ID={job.id}",
                "-e",
                "CI=true",
                *docker_cli,
            ]
            for key, value in contract.env.items():
                argv += ["-e", f"{key}={value}"]
            argv += [image, self.shell, "-lc", "sh /workspace/run.sh"]
            code = self.runner.run(argv, log, cancel=cancel)
            return 130 if cancel.is_set() else code
        finally:
            for name in services:
                self.runner.run([self.docker, "rm", "-f", name], log)
            self.runner.run([self.docker, "network", "rm", network], log)

    def _docker_cli(self, contract: Contract) -> list[str]:
        """Socket and CLI mounts for contracts whose steps build or run images."""
        if not contract.docker:
            return []
        socket = Path(DOCKER_SOCKET)
        if not socket.exists():
            raise ExecutorError(f"contract requests docker but {DOCKER_SOCKET} is missing")
        binary = shutil.which(self.docker)
        if binary is None:
            raise ExecutorError(f"contract requests docker but {self.docker} is not on PATH")
        resolved = str(Path(binary).resolve())
        mounts = [
            "-v",
            f"{socket}:{socket}",
            "--group-add",
            str(socket.stat().st_gid),
            "-v",
            f"{resolved}:{resolved}:ro",
        ]
        for directory in DOCKER_PLUGIN_DIRS:
            path = Path(directory)
            if path.is_dir():
                mounts += ["-v", f"{path}:{path}:ro"]
        return mounts

    def _wait_for_services(
        self,
        waiting: list[tuple[str, Service]],
        log: TextIO,
        cancel: threading.Event,
    ) -> bool:
        if not waiting:
            return True
        for _, service in waiting:
            log.write(f"cindral: waiting for {service.name} to become healthy\n")
        deadline = time.monotonic() + SERVICE_HEALTH_TIMEOUT
        pending = waiting
        while pending:
            remaining: list[tuple[str, Service]] = []
            for name, service in pending:
                code, status = self.runner.capture(
                    [self.docker, "inspect", "--format", "{{.State.Health.Status}}", name]
                )
                if code != 0 or status.strip() != "healthy":
                    remaining.append((name, service))
            if not remaining:
                return True
            if cancel.is_set():
                return False
            if time.monotonic() >= deadline:
                names = ", ".join(service.name for _, service in remaining)
                raise ExecutorError(f"services did not become healthy within {SERVICE_HEALTH_TIMEOUT}s: {names}")
            pending = remaining
            time.sleep(SERVICE_HEALTH_POLL_SECONDS)
        return True

    def _script(self, steps: tuple[str, ...]) -> str:
        lines = ["set -e"]
        for index, step in enumerate(steps, start=1):
            lines.append(f"printf '\\ncindral: step {index}: %s\\n' {shlex.quote(step)}")
            lines.append(step)
        return "\n".join(lines) + "\n"

    def _git_env(self) -> dict[str, str] | None:
        if not self.token:
            return None
        basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        env = dict(os.environ)
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {basic}"
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

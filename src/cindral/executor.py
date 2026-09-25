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
from pathlib import Path
from typing import TextIO

from .agent import JobSpec
from .contract import CONTRACT_PATH, Contract, ContractError
from .models import ExecutionResult

MAX_LOG_CHARS = 64 * 1024


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

    def __call__(self, job: JobSpec, cancel: threading.Event) -> ExecutionResult:
        workspace = self.workspace_root / job.id
        log_path = (self.log_dir / f"{job.id}.log") if self.log_dir else None
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
                shutil.rmtree(workspace, ignore_errors=True)
        text = log_path.read_text(errors="replace") if log_path else ""
        return ExecutionResult(exit_code=exit_code, log=tail(text))

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
        network = f"cindral-{job.id}"
        # one container per job, so installs persist across steps the way they
        # do in a single CI job; the checkout is mounted from the workspace
        (workspace / "run.sh").write_text(self._script(contract.steps))
        services: list[str] = []
        self.runner.check([self.docker, "network", "create", network], log, cancel=cancel)
        try:
            for service in contract.services:
                name = f"{network}-{service.name}"
                argv = [self.docker, "run", "-d", "--name", name, "--network", network, "--network-alias", service.name]
                for key, value in service.env.items():
                    argv += ["-e", f"{key}={value}"]
                argv += [service.image, *service.command]
                self.runner.check(argv, log, cancel=cancel)
                services.append(name)
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

"""On-device bounded-Docker executor for direct jobs.

Reads the repository contract (.cindral/ci.toml) from the checked-out commit,
runs the steps in a container on an isolated network with declared sidecars, and
returns the exit code plus captured output. One instance serves every slot the
agent runs concurrently, so only the build probe writes to it after construction.
"""
import base64
import os
import secrets
import shlex
import shutil
import threading
import time
from pathlib import Path
from typing import TextIO

from .agent import JobSpec
from .artifact_pack import pack_artifacts
from .cache_adapters import run_cached_job
from .cache_client import CindralCacheClient
from .actions import HOSTED_LABEL_SHAPE
from .contract import Contract, ContractError, Service
from .inference import resolve_contract
from .limits import JobLimits
from .docker_support import docker_cli_mounts, make_cache_readable
from .models import ExecutionResult
from .process import ExecutorError, ProcessRunner, tail
from .steps import completed_log, run_script

SERVICE_HEALTH_TIMEOUT = 90
SERVICE_HEALTH_POLL_SECONDS = 2
# below this a quota rounds to a fraction of a millisecond of cpu per period, so
# the container makes no progress and the cap is a mistake rather than a limit
MIN_JOB_CPUS = 0.01

# re-exported because the executor is where a caller reaches for the process
# helpers it runs, and they now live beside what they are
__all__ = ["ExecutorError", "ProcessRunner", "tail"]


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
        cache_client: CindralCacheClient | None = None,
        cpus: float | None = None,
        memory: str | None = None,
        disk_write: int | None = None,
        disk_read: int | None = None,
        device_labels: tuple[str, ...] | list[str] = (),
    ) -> None:
        if cpus is not None and cpus < MIN_JOB_CPUS:
            raise ExecutorError(f"a cpu cap of {cpus} is too small to run anything; the minimum is {MIN_JOB_CPUS}")
        self.token = token or ""
        self.workspace_root = Path(workspace_root)
        self.log_dir = Path(log_dir) if log_dir else None
        self.docker = docker
        self.git = git
        self.github_base = github_base.rstrip("/")
        self.runner = runner or ProcessRunner()
        self.shell = shell
        self.cleanup = cleanup
        self.cache_client = cache_client
        self.limits = JobLimits(
            cpus=cpus,
            memory=memory,
            disk_write=disk_write,
            disk_read=disk_read,
        )
        self.device_labels = {str(label).strip().lower() for label in device_labels}
        self._build_style: str | None = None
        self._build_lock = threading.Lock()

    def __call__(self, job: JobSpec, cancel: threading.Event) -> ExecutionResult:
        workspace = self.workspace_root / job.id
        # One nonce per run, and a local rather than instance state: one executor
        # serves every slot on the device, so a nonce held on self would be
        # reassigned by a concurrent claim and the run would be parsed against
        # another run's markers, yielding no steps at all.
        nonce = secrets.token_hex(16)
        log_path = (self.log_dir / f"{job.id}.log") if self.log_dir else None
        cleanup_image: str | None = None
        if log_path is None:
            log_handle: TextIO = open(os.devnull, "w")
        else:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_path.open("w")
        try:
            with log_handle as log:
                exit_code, cleanup_image, archive = self._execute(job, cancel, workspace, log, nonce)
        finally:
            cleanup_warning = self._cleanup_workspace(workspace, cleanup_image) if self.cleanup else None
        return completed_log(log_path, exit_code, cleanup_warning, limit=tail, nonce=nonce, archive=archive)

    def _remove_workspace(self, workspace: Path, suffix: str = "") -> str | None:
        try:
            shutil.rmtree(workspace)
            return None
        except OSError as exc:
            # a missing workspace is already gone; anything else needs reporting
            if workspace.exists():
                return f"could not remove workspace {workspace}{suffix}: {exc}"
            return None

    def _cleanup_workspace(self, workspace: Path, image: str | None) -> str | None:
        failure = self._remove_workspace(workspace)
        if failure is None or image is None:
            return failure
        command = [
            self.docker, "run", "--rm", "--network", "none", "--user", "0:0", "-v",
            f"{workspace}:/cleanup", *self.limits.run_args(), "--entrypoint", self.shell, image, "-lc",
            "rm -rf /cleanup/* /cleanup/.[!.]* /cleanup/..?*",
        ]
        with open(os.devnull, "w") as log:
            code = self.runner.run(command, log)
        if code != 0:
            return f"could not remove root-owned files from workspace {workspace} (cleanup container exited {code})"
        return self._remove_workspace(workspace, " after container cleanup")

    def _execute(
        self,
        job: JobSpec,
        cancel: threading.Event,
        workspace: Path,
        log: TextIO,
        nonce: str,
    ) -> tuple[int, str | None, str]:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir(parents=True)
        repo = workspace / "repo"
        args = self.limits.run_args()
        caps = f" caps {' '.join(args)}" if args else ""
        # held outside the try so a failure after the image is known still hands
        # cleanup an image; otherwise root-owned files stay behind
        image: str | None = None
        try:
            log.write(f"cindral: job {job.id} {job.repository}@{job.sha[:12]}{caps}\n")
            self._checkout(job, repo, log, cancel)
            contract = self._load_contract(repo, job, log)
            image = self._image(job, repo, contract, log, cancel)
            make_readable = lambda path: make_cache_readable(
                self.runner, self.docker, path, image, log, cancel, self.shell
            )
            code = run_cached_job(
                job, repo, workspace, log, cancel, self.cache_client,
                lambda cache_root, cache_environment: self._run_steps(
                    job, contract, image, workspace, log, cancel, nonce, cache_root, cache_environment
                ),
                make_readable,
            )
            return code, image, pack_artifacts(repo, workspace, log, make_readable)
        except ExecutorError as exc:
            log.write(f"cindral: {exc}\n")
            return 1, image, ""
        except Exception as exc:  # noqa: BLE001 - report any device failure as a job failure
            log.write(f"cindral: unexpected executor failure: {exc!r}\n")
            return 1, image, ""

    def _load_contract(self, repo: Path, job: JobSpec, log: TextIO | None = None) -> Contract:
        try:
            contract = resolve_contract(repo, job, note=log.write if log is not None else None)
        except ContractError as exc:
            raise ExecutorError(str(exc)) from exc
        self._check_labels(contract, log)  # type: ignore[arg-type]
        return contract

    def _check_labels(self, contract: Contract, log: TextIO) -> None:
        """Refuse a job whose hardware this device does not have.

        An image is not the whole of what `runs-on` asks for. `[self-hosted, ARM64,
        repo-label]` is a statement about the machine, and resolving that to an image
        alone would let the job run on hardware satisfying none of it and report
        success. The claim already matched the job's labels, but the broker enqueues
        without them, so this is the first place both sides are known.
        """
        # Only custom labels are device labels. `ubuntu-24.04` is an image, already
        # settled by the translator, and no device advertises it; enforcing it would
        # refuse every hosted job on a correctly configured pool.
        advertised = getattr(self, "device_labels", frozenset())
        missing = [
            label
            for label in contract.labels
            if not HOSTED_LABEL_SHAPE.match(label)
            and label.strip().lower() not in advertised
        ]
        if missing:
            raise ExecutorError(
                f"this device does not advertise {sorted(missing)}, and the job asks for it;"
                f" it advertises {sorted(advertised) or 'nothing'}"
            )

    def _checkout(self, job: JobSpec, repo: Path, log: TextIO, cancel: threading.Event) -> None:
        url = f"{self.github_base}/{job.repository}.git"
        env = self._git_env()
        repo.mkdir(parents=True)
        self.runner.check([self.git, "init", "-q", str(repo)], log, cancel=cancel)
        self.runner.check([self.git, "-C", str(repo), "remote", "add", "origin", url], log, cancel=cancel)
        fetch = [self.git, "-C", str(repo), "fetch", "--depth", "1", "origin", job.sha]
        # fetching a bare sha is not always allowed; fall back to the ref
        if self.runner.run(fetch, log, env=env, cancel=cancel) != 0:
            self.runner.check([self.git, "-C", str(repo), "fetch", "origin", job.ref], log, env=env, cancel=cancel)
        self.runner.check([self.git, "-C", str(repo), "checkout", "--detach", job.sha], log, env=env, cancel=cancel)

    def _image(self, job: JobSpec, repo: Path, contract: Contract, log: TextIO, cancel: threading.Event) -> str:
        if contract.dockerfile:
            tag = f"cindral-job-{job.id}"
            dockerfile = repo / contract.dockerfile
            if not dockerfile.is_file():
                raise ExecutorError(f"dockerfile not found: {contract.dockerfile}")
            build = [self.docker, "build", *self._build_limit_args(log), "-t", tag]
            self.runner.check(build + ["-f", str(dockerfile), str(repo)], log, cancel=cancel)
            return tag
        assert contract.image is not None
        if self.runner.run([self.docker, "image", "inspect", contract.image], log, cancel=cancel) != 0:
            self.runner.check([self.docker, "pull", contract.image], log, cancel=cancel)
        return contract.image

    def _build_limit_args(self, log: TextIO) -> list[str]:
        """The job caps in the form this `docker build` accepts.

        BuildKit spells them as repeatable --resource key=value and the legacy
        builder takes the run flags; a CLI with neither leaves the build uncapped
        rather than failing the job on an unknown flag. The probe is cached, the
        warning is not, so every uncapped build says so in its own log.
        """
        note = self.limits.build_disk_note()
        if note is not None:
            log.write(note + "\n")
            log.flush()
        if not self.limits.capped:
            return []
        with self._build_lock:
            if self._build_style is None:
                _, help_text = self.runner.capture([self.docker, "build", "--help"])
                if "--resource" in help_text:
                    self._build_style = "resource"
                elif "--cpu-quota" in help_text:
                    self._build_style = "legacy"
                else:
                    self._build_style = ""
        if not self._build_style:
            log.write("cindral: warning: docker build has no CPU or memory flag here, the build is uncapped\n")
            return []
        return self.limits.build_args(resource=self._build_style == "resource")

    def describe_limits(self) -> str:
        """The caps this device applies, for the line printed when it starts."""
        return self.limits.describe()

    def _run_steps(
        self,
        job: JobSpec,
        contract: Contract,
        image: str,
        workspace: Path,
        log: TextIO,
        cancel: threading.Event,
        nonce: str,
        cache_root: Path | None = None,
        cache_environment: dict[str, str] | None = None,
    ) -> int:
        try:
            docker_cli = docker_cli_mounts(self.docker) if contract.docker else []
        except ValueError as exc:
            raise ExecutorError(str(exc)) from exc
        network = f"cindral-{job.id}"
        # one container per job, so installs persist across steps the way they
        # do in a single CI job; the checkout is mounted from the workspace
        (workspace / "run.sh").write_text(self._script(contract.steps, nonce))
        services: list[str] = []
        unhealthy: list[tuple[str, Service]] = []
        self.runner.check([self.docker, "network", "create", network], log, cancel=cancel)
        timeout = min(job.timeout, contract.timeout_minutes * 60) if contract.timeout_minutes else job.timeout
        timer = threading.Timer(timeout, cancel.set) if timeout else None
        if timer:
            timer.start()
        try:
            for service in contract.services:
                name = f"{network}-{service.name}"
                argv = [
                    self.docker, "run", "-d", "--name", name, "--network", network,
                    "--network-alias", service.name, *self.limits.run_args(),
                ]
                if service.health_cmd:
                    argv += [
                        "--health-cmd", service.health_cmd, "--health-interval", "2s",
                        "--health-timeout", "5s", "--health-retries", "30",
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
            argv = [self.docker, "run", "--name", f"{network}-job", "--network", network]
            argv += ["-v", f"{workspace}:/workspace", "-w", "/workspace/repo"]
            argv += ["-e", f"CINDRAL_JOB_ID={job.id}", "-e", "CI=true"]
            argv += [*self.limits.run_args(), *docker_cli]
            if cache_root is not None:
                argv += ["-v", f"{cache_root}:/cindral-cache"]
            for key, value in {**contract.env, **(cache_environment or {})}.items():
                argv += ["-e", f"{key}={value}"]
            argv += [image, self.shell, "-lc", "sh /workspace/run.sh"]
            code = self.runner.run(argv, log, cancel=cancel)
            return 130 if cancel.is_set() else code
        finally:
            if timer:
                timer.cancel()
            # a killed `docker run` client leaves its container up, so --rm alone is not enough
            for name in [f"{network}-job", *services]:
                self.runner.run([self.docker, "rm", "-f", name], log)
            self.runner.run([self.docker, "network", "rm", network], log)

    def _healthy(self, name: str) -> bool:
        code, status = self.runner.capture([self.docker, "inspect", "--format", "{{.State.Health.Status}}", name])
        return code == 0 and status.strip() == "healthy"

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
            pending = [pair for pair in pending if not self._healthy(pair[0])]
            if not pending:
                return True
            if cancel.is_set():
                return False
            if time.monotonic() >= deadline:
                names = ", ".join(service.name for _, service in pending)
                raise ExecutorError(f"services did not become healthy within {SERVICE_HEALTH_TIMEOUT}s: {names}")
            time.sleep(SERVICE_HEALTH_POLL_SECONDS)
        return True

    def _script(self, steps: tuple[str, ...], nonce: str) -> str:
        return run_script(steps, nonce)

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

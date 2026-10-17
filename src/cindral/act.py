"""GitHub Actions compatibility engine: a workflow job executed by act.

act reads the workflow file, downloads the actions the job names, and runs every
step in a container it creates through the device Docker socket. A job on this
path therefore belongs to the same trust tier as a contract that sets
`runner.docker = true`, and only reaches this module after the direct-execution
trust gate admitted the repository.
"""
import json
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contract import Contract

# The engine release and the checksums act publishes for its tarballs are pinned in
# the act stage of the Dockerfile; a test fails when this moves without it.
ACT_VERSION = "0.2.89"
ACT_IMAGE = f"cindral-act:{ACT_VERSION}"
ACT_BINARY = "/usr/local/bin/act"
# where the executor mounts the workspace and the checkout inside the job container
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_REPO = f"{CONTAINER_WORKSPACE}/repo"
EVENT_PATH = f"{CONTAINER_WORKSPACE}/cindral-act-event.json"


def job_network(job_id: str) -> str:
    """The per-job network the executor creates, which act puts its containers on.

    Mirrors the executor's own naming, and a test runs the executor to fail when
    the two stop agreeing, since act would then start containers on a network the
    executor did not create and tears down.
    """
    return f"cindral-{job_id}"


def container_workflow_path(workflow_path: Path, repo: Path) -> str:
    """The workflow path as the engine container sees the checkout."""
    try:
        relative = workflow_path.relative_to(repo)
    except ValueError as exc:
        raise ValueError(f"workflow {workflow_path} is not inside the checkout {repo}") from exc
    return f"{CONTAINER_REPO}/{relative.as_posix()}"


def build_act_command(
    act: str,
    workflow_path: str,
    workdir: str,
    job_id: str,
    event_name: str,
    event_path: str | None = None,
    platforms: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
    network: str | None = None,
) -> list[str]:
    """The act argv that runs one workflow job inside the engine container."""
    argv = [act, event_name, "-W", workflow_path, "-C", workdir, "-j", job_id]
    if event_path is not None:
        argv += ["--eventpath", event_path]
    # the daemon resolves a bind source on the host, where the executor's mount
    # point does not exist, so act copies the checkout in instead of binding it
    argv += ["--bind=false"]
    # the engine keeps the socket to itself, so a step that shells out to Docker
    # fails loudly rather than inheriting the device's root-equivalent daemon
    argv += ["--container-daemon-socket", "-"]
    if network:
        argv += ["--network", network]
    for label, image in sorted((platforms or {}).items()):
        argv += ["-P", f"{label}={image}"]
    for key, value in sorted((env or {}).items()):
        argv += ["--env", f"{key}={value}"]
    return argv


@dataclass(frozen=True)
class ActJob:
    """One workflow job, resolved against the checkout the executor already made."""

    workflow_path: Path
    repo: Path
    job_id: str
    event_name: str
    event: dict[str, Any]
    reason: str
    platforms: dict[str, str]
    steps: tuple[dict[str, Any], ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    network: str | None = None
    timeout_minutes: int | None = None


def act_contract(request: ActJob) -> Contract:
    """The contract that runs one workflow job through the act engine.

    The engine container is the only container the executor starts, and it needs
    the daemon socket, so the contract asks for the same socket mount a contract
    sets as `runner.docker = true`: the job fails before any step on a device
    without one. The workflow's own environment reaches the step containers
    through act rather than the engine container, where it could point the engine
    at a different Docker daemon.
    """
    argv = build_act_command(
        act=ACT_BINARY,
        workflow_path=container_workflow_path(request.workflow_path, request.repo),
        workdir=CONTAINER_REPO,
        job_id=request.job_id,
        event_name=request.event_name,
        event_path=EVENT_PATH,
        platforms=request.platforms,
        env=request.env,
        network=request.network,
    )
    return Contract(
        steps=(_script(request, argv),),
        image=ACT_IMAGE,
        timeout_minutes=request.timeout_minutes,
        docker=True,
    )


def action_references(steps: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Every action the job names, which is third-party code act downloads and runs."""
    return tuple(str(step["uses"]).strip() for step in steps if step.get("uses") is not None)


def _script(request: ActJob, argv: list[str]) -> str:
    lines = [
        _echo(
            f"cindral: act {ACT_VERSION} ({ACT_IMAGE}) runs job '{request.job_id}' "
            f"of {request.workflow_path.name}: {request.reason}"
        ),
        _echo(f"cindral: act actions: {', '.join(action_references(request.steps)) or 'none'}"),
        f"printf '%s' {shlex.quote(json.dumps(request.event, sort_keys=True))} > {shlex.quote(EVENT_PATH)}",
        "exec " + " ".join(shlex.quote(part) for part in argv),
    ]
    return "\n".join(lines)


def _echo(message: str) -> str:
    return f"printf '%s\\n' {shlex.quote(message)}"

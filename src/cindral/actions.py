"""GitHub Actions workflow discovery, parsing, and translation."""
from dataclasses import dataclass, field
import fnmatch
from pathlib import Path
from typing import Any

import yaml

from .agent import JobSpec
from .contract import Contract, ContractError, Service


DEFAULT_RUNNER_IMAGE = "ubuntu:24.04"
IMAGE_MAP: dict[str, str] = {
    "ubuntu-latest": "ubuntu:24.04",
    "ubuntu-24.04": "ubuntu:24.04",
    "ubuntu-22.04": "ubuntu:22.04",
    "ubuntu-20.04": "ubuntu:20.04",
}


class WorkflowError(ContractError):
    """Raised when a GitHub Actions workflow cannot be parsed or matched."""


@dataclass(frozen=True)
class WorkflowJob:
    id: str
    name: str
    runs_on: tuple[str, ...]
    steps: tuple[dict[str, Any], ...]
    env: dict[str, str] = field(default_factory=dict)
    services: tuple[Service, ...] = ()
    timeout_minutes: int | None = None


@dataclass(frozen=True)
class Workflow:
    path: Path
    name: str
    on: Any
    jobs: dict[str, WorkflowJob]


def parse_workflow(path: Path) -> Workflow:
    try:
        content = path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
    except (OSError, yaml.YAMLError) as exc:
        raise WorkflowError(f"invalid workflow YAML in {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowError(f"workflow {path.name} must be a YAML mapping")

    name = str(data.get("name", path.stem))
    on_spec = data.get("on") or data.get(True) or {}
    raw_env = data.get("env", {})
    workflow_env = _string_map(raw_env if isinstance(raw_env, dict) else {}, f"{path.name}.env")

    raw_jobs = data.get("jobs", {})
    if not isinstance(raw_jobs, dict) or not raw_jobs:
        raise WorkflowError(f"workflow {path.name} must define at least one job")

    jobs: dict[str, WorkflowJob] = {}
    for job_id, raw_job in raw_jobs.items():
        if not isinstance(raw_job, dict):
            continue
        job_name = str(raw_job.get("name", job_id))

        raw_runs_on = raw_job.get("runs-on", "ubuntu-latest")
        if isinstance(raw_runs_on, str):
            runs_on = (raw_runs_on,)
        elif isinstance(raw_runs_on, list):
            runs_on = tuple(str(item) for item in raw_runs_on)
        else:
            runs_on = ("ubuntu-latest",)

        job_env = dict(workflow_env)
        raw_job_env = raw_job.get("env", {})
        if isinstance(raw_job_env, dict):
            job_env.update(_string_map(raw_job_env, f"{path.name}.jobs.{job_id}.env"))

        raw_services = raw_job.get("services", {})
        services: list[Service] = []
        if isinstance(raw_services, dict):
            for svc_name, svc_data in raw_services.items():
                if isinstance(svc_data, dict) and "image" in svc_data:
                    svc_env = _string_map(
                        svc_data.get("env", {}) if isinstance(svc_data.get("env"), dict) else {},
                        f"{svc_name}.env",
                    )
                    services.append(
                        Service(
                            name=str(svc_name).lower(),
                            image=str(svc_data["image"]).strip(),
                            env=svc_env,
                        )
                    )

        raw_steps = raw_job.get("steps", [])
        steps: list[dict[str, Any]] = []
        if isinstance(raw_steps, list):
            for step in raw_steps:
                if isinstance(step, dict):
                    steps.append(step)

        if "if" in raw_job and raw_job["if"] not in (True, "true", None):
            raise WorkflowError(f"job '{job_id}' uses unsupported 'if' condition (requires act engine)")

        raw_timeout = raw_job.get("timeout-minutes")
        if raw_timeout is not None:
            if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, str)):
                raise WorkflowError(f"job '{job_id}' timeout-minutes must be a positive integer")
            try:
                timeout_val = int(raw_timeout)
                if timeout_val <= 0:
                    raise ValueError
            except ValueError:
                raise WorkflowError(f"job '{job_id}' timeout-minutes must be a positive integer")
            timeout_minutes: int | None = timeout_val
        else:
            timeout_minutes = None

        jobs[job_id] = WorkflowJob(
            id=job_id,
            name=job_name,
            runs_on=runs_on,
            steps=tuple(steps),
            env=job_env,
            services=tuple(services),
            timeout_minutes=timeout_minutes,
        )

    return Workflow(path=path, name=name, on=on_spec, jobs=jobs)


def find_workflows(repo: Path) -> list[Workflow]:
    workflows_dir = repo / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return []
    workflows: list[Workflow] = []
    for path in sorted(workflows_dir.iterdir()):
        if path.is_file() and path.suffix in {".yml", ".yaml"} and not path.name.startswith("."):
            workflows.append(parse_workflow(path))
    return workflows


def _patterns(value: Any) -> list[str] | None:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return None


def event_matches(
    on_spec: Any,
    event_name: str,
    branch_or_tag: str,
    base_ref: str | None = None,
) -> bool:
    if isinstance(on_spec, str):
        return on_spec == event_name
    if isinstance(on_spec, list):
        return any(event_matches(item, event_name, branch_or_tag, base_ref) for item in on_spec)
    if isinstance(on_spec, dict):
        if event_name not in on_spec:
            return False
        config = on_spec.get(event_name)
        if config is None:
            return True
        if isinstance(config, dict):
            is_tag = branch_or_tag.startswith("refs/tags/")
            clean_ref = (
                branch_or_tag.removeprefix("refs/tags/")
                if is_tag
                else branch_or_tag.removeprefix("refs/heads/")
            )
            has_tag_filter = "tags" in config or "tags-ignore" in config
            has_branch_filter = "branches" in config or "branches-ignore" in config
            if is_tag:
                if has_branch_filter and not has_tag_filter:
                    return False
                tags = _patterns(config.get("tags"))
                tags_ignore = _patterns(config.get("tags-ignore"))
                if tags is not None and not any(fnmatch.fnmatch(clean_ref, p) for p in tags):
                    return False
                if tags_ignore is not None and any(fnmatch.fnmatch(clean_ref, p) for p in tags_ignore):
                    return False
                return True
            if has_tag_filter and not has_branch_filter:
                return False
            if event_name == "pull_request":
                is_pr_ref = branch_or_tag.startswith("refs/pull/") or branch_or_tag.startswith("pull/")
                if is_pr_ref and base_ref is None:
                    return True
                target = base_ref if base_ref is not None else clean_ref
                branches = _patterns(config.get("branches"))
                branches_ignore = _patterns(config.get("branches-ignore"))
                if branches is not None and not any(fnmatch.fnmatch(target, p) for p in branches):
                    return False
                if branches_ignore is not None and any(fnmatch.fnmatch(target, p) for p in branches_ignore):
                    return False
                return True
            branches = _patterns(config.get("branches"))
            branches_ignore = _patterns(config.get("branches-ignore"))
            if branches is not None and not any(fnmatch.fnmatch(clean_ref, p) for p in branches):
                return False
            if branches_ignore is not None and any(fnmatch.fnmatch(clean_ref, p) for p in branches_ignore):
                return False
        return True
    return False


def resolve_workflow_job(repo: Path, job: JobSpec) -> tuple[Workflow, WorkflowJob]:
    workflows = find_workflows(repo)
    if not workflows:
        raise WorkflowError("repository has no .cindral/ci.toml and no .github/workflows/*.{yml,yaml}")

    is_pr = job.ref.startswith("refs/pull/") or job.ref.startswith("pull/")
    event_name = "pull_request" if is_pr else "push"
    target_job = job.command[0] if job.command else None

    matching: list[tuple[Workflow, WorkflowJob]] = []
    for wf in workflows:
        if not event_matches(wf.on, event_name, job.ref):
            continue
        for j_id, j in wf.jobs.items():
            matches_target = (
                target_job is None
                or target_job in {j_id, j.name}
                or target_job == f"{wf.path.name}:{j_id}"
                or (target_job == wf.path.name and len(wf.jobs) == 1)
            )
            if matches_target:
                matching.append((wf, j))

    if not matching:
        target_clause = f" for target '{target_job}'" if target_job else ""
        raise WorkflowError(
            f"no workflow job matched event '{event_name}' on ref '{job.ref}'{target_clause}"
        )

    if len(matching) > 1:
        if target_job is None:
            preferred = [m for m in matching if m[1].id in {"ci", "test"} or m[1].name.lower() in {"ci", "test"}]
            if len(preferred) == 1:
                return preferred[0]
        job_list = ", ".join(f"{wf.path.name}:{j.id}" for wf, j in matching)
        raise WorkflowError(f"multiple workflow jobs matched ({job_list}); specify an unambiguous target job")

    return matching[0]


def workflow_to_contract(job: WorkflowJob) -> Contract:
    steps: list[str] = []
    for step in job.steps:
        if "if" in step and step["if"] not in (True, "true", None):
            raise WorkflowError(f"step uses unsupported 'if' condition (requires act engine)")
        if "working-directory" in step:
            raise WorkflowError(f"step uses unsupported 'working-directory' (requires act engine)")
        if "run" in step:
            run_cmd = str(step["run"]).strip()
            if run_cmd:
                steps.append(run_cmd)
        elif "uses" in step:
            action = str(step["uses"]).strip()
            action_repo = action.split("@")[0]
            if action_repo == "actions/checkout":
                continue
            raise WorkflowError(f"step uses unsupported action '{action}' (requires act engine)")

    if not steps:
        raise WorkflowError(f"job '{job.id}' has no executable steps")

    image = None
    for label in job.runs_on:
        if label in IMAGE_MAP:
            image = IMAGE_MAP[label]
            break
    if image is None:
        raise WorkflowError(f"job '{job.id}' specifies unsupported runs-on '{job.runs_on}'")

    return Contract(
        steps=tuple(steps),
        image=image,
        env=job.env,
        services=job.services,
        timeout_minutes=job.timeout_minutes,
    )


def resolve_workflow_contract(repo: Path, job: JobSpec) -> Contract:
    _, workflow_job = resolve_workflow_job(repo, job)
    return workflow_to_contract(workflow_job)


def create_event_payload(job: JobSpec, event_name: str) -> dict[str, Any]:
    repo_parts = job.repository.split("/", 1)
    owner = repo_parts[0] if len(repo_parts) > 1 else ""
    name = repo_parts[1] if len(repo_parts) > 1 else job.repository
    is_pr = job.ref.startswith("refs/pull/") or job.ref.startswith("pull/")
    clean_branch = job.ref.removeprefix("refs/heads/")
    pr_number = 1
    if is_pr:
        clean_pr = job.ref.removeprefix("refs/pull/").removeprefix("pull/")
        parts = clean_pr.split("/")
        if parts and parts[0].isdigit():
            pr_number = int(parts[0])

    payload: dict[str, Any] = {
        "repository": {
            "full_name": job.repository,
            "name": name,
            "owner": {"login": owner},
            "default_branch": "main",
        },
        "ref": job.ref,
        "sha": job.sha,
        "action": "opened" if event_name == "pull_request" else None,
    }
    if event_name == "pull_request" or is_pr:
        payload["action"] = "opened"
        payload["pull_request"] = {
            "number": pr_number,
            "head": {"sha": job.sha, "ref": clean_branch},
            "base": {"ref": "main"},
        }
    return payload


def build_act_command(
    act: str,
    workflow_path: Path,
    job_id: str,
    event_name: str,
    event_path: Path | None = None,
    image_map: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
) -> list[str]:
    cmd = [
        act,
        event_name,
        "-W",
        str(workflow_path),
        "-j",
        job_id,
    ]
    if event_path is not None:
        cmd.extend(["--eventpath", str(event_path)])
    mapping = image_map or IMAGE_MAP
    for label, img in mapping.items():
        cmd.extend(["-P", f"{label}={img}"])
    if env:
        for k, v in sorted(env.items()):
            cmd.extend(["--env", f"{k}={v}"])
    return cmd


def _string_map(raw: dict[Any, Any], context: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            raise WorkflowError(f"{context} keys must be strings")
        result[k] = str(v)
    return result

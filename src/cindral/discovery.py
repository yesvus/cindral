"""Finding and parsing a repository's committed workflow files.

Kept apart from the translation in `actions` so that a change to how a workflow
is read does not have to share a file with how it is run.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .contract import Service
from .workflow_errors import (
    WorkflowError,
    WorkflowNoMatch,
    WorkflowAmbiguous,
    WorkflowUnsupported,
)
from .workflow_inputs import (
    _as_expression,
    _call_inputs,
    _caller_inputs,
    _guard_inputs,
    _plain_inputs,
    _substitute_inputs,
)

# The errors were defined here until they were shared with the input reader, and
# callers still import them from this module.
__all__ = [
    "Workflow",
    "WorkflowError",
    "WorkflowJob",
    "WorkflowNoMatch",
    "WorkflowAmbiguous",
    "WorkflowUnsupported",
    "find_workflows",
    "is_workflow_file",
    "parse_workflow",
    "parseable_workflows",
    "workflow_files",
]


# job keys this build refuses rather than run part of the job. Defined here
# because the parse detects them; `actions` names the one it refused, so both
# must read this single definition.
UNMAPPED_JOB_KEYS = {
    "needs": "job dependencies",
    "strategy": "matrix fan-out",
    "secrets": "secrets",
    "defaults": "step defaults",
    "container": "job container",
}


@dataclass(frozen=True)
class WorkflowJob:
    id: str
    name: str
    runs_on: tuple[str, ...]
    steps: tuple[dict[str, Any], ...]
    env: dict[str, str] = field(default_factory=dict)
    services: tuple[Service, ...] = ()
    timeout_minutes: int | None = None
    unmapped: tuple[str, ...] = ()
    condition: str | None = None


@dataclass(frozen=True)
class Workflow:
    path: Path
    name: str
    on: Any
    jobs: dict[str, WorkflowJob]


def parse_workflow(path: Path, repo: Path | None = None) -> Workflow:
    try:
        content = path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
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
    raw_jobs = _expand_calls(
        raw_jobs,
        repo if repo is not None else path.parents[2],
        frozenset({path.resolve()}),
        _caller_inputs(on_spec),
    )

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
            unmapped=tuple(key for key in UNMAPPED_JOB_KEYS if key in raw_job),
            condition=_condition_of(raw_job),
        )

    return Workflow(path=path, name=name, on=on_spec, jobs=jobs)


def _expand_calls(
    raw_jobs: dict[Any, Any],
    repo: Path,
    seen: frozenset[Path],
    caller_inputs: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Replace a reusable workflow call with the jobs of the workflow it calls.

    A repository whose entry job delegates to a reusable workflow declares no
    steps of its own, so reading only the file the run names finds a job that
    cannot run. The called workflow's jobs are inlined under the calling job's
    id, which keeps two calls of the same workflow from colliding, and the
    caller's `with:` values are substituted for the `inputs.` the called
    workflow reads.
    """
    expanded: dict[str, Any] = {}
    for job_id, raw_job in raw_jobs.items():
        if not isinstance(raw_job, dict) or not isinstance(raw_job.get("uses"), str):
            expanded[str(job_id)] = raw_job
            continue
        called = raw_job["uses"]
        if not called.startswith("./"):
            raise WorkflowUnsupported(
                f"job '{job_id}' calls the reusable workflow '{called}', which is not in this repository"
            )
        try:
            called_path = (repo / called.removeprefix("./")).resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkflowUnsupported(
                f"job '{job_id}' calls '{called}', which could not be resolved: {exc}"
            ) from exc
        if not _inside(called_path, repo):
            raise WorkflowUnsupported(
                f"job '{job_id}' calls '{called}', which reaches outside the repository"
            )
        if called_path in seen:
            raise WorkflowUnsupported(
                f"job '{job_id}' calls '{called}', which is already being read"
            )
        for key in UNMAPPED_JOB_KEYS:
            if key in raw_job:
                raise WorkflowUnsupported(
                    f"job '{job_id}' calls a reusable workflow and declares {UNMAPPED_JOB_KEYS[key]} ('{key}')"
                )
        called_jobs, called_env = _read_call(called_path, called, job_id, raw_job.get("with"))
        # The recursion is given no caller inputs: `_read_call` has already put
        # the call's own `with:` values into every guard the called workflow
        # carries, or refused the name, so a nested guard never needs this map
        # and passing it would suggest a path that does not exist.
        for called_id, called_job in _expand_calls(
            called_jobs, repo, seen | {called_path}
        ).items():
            if not isinstance(called_job, dict):
                continue
            merged = dict(called_job)
            merged["env"] = {**called_env, **(merged.get("env") or {})}
            # The caller's own condition is read against the caller's inputs, not
            # the called workflow's. A call guarded by `if: ${{ inputs.run-ci }}`
            # arrives with the expression intact, and the condition parser then
            # refuses a job whose guard was never resolved.
            guard = _condition_of(raw_job)
            if guard is not None and caller_inputs is not None:
                guard = _substitute_inputs(
                    guard, caller_inputs, f"the 'if' of job '{job_id}'"
                )
            merged["if"] = _combined_condition(guard, _condition_of(called_job))
            expanded[f"{job_id}/{called_id}"] = merged
    return expanded


def _read_call(
    called_path: Path, called: str, job_id: Any, with_inputs: Any
) -> tuple[dict[Any, Any], dict[str, str]]:
    try:
        data = yaml.safe_load(called_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise WorkflowUnsupported(f"job '{job_id}' calls '{called}', which cannot be read: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
        raise WorkflowUnsupported(f"job '{job_id}' calls '{called}', which defines no jobs")
    called_jobs = data["jobs"]
    # PyYAML resolves an `on:` key to the boolean True, which `parse_workflow`
    # compensates for and this did not. So the called workflow's declared input
    # defaults never loaded, and every input it declared a default for was
    # refused by name as one the caller failed to pass.
    declared = _call_inputs(data.get("on") or data.get(True) or {})
    overrides = (
        _string_map(with_inputs, f"the call to '{called}' from job '{job_id}'")
        if isinstance(with_inputs, dict) else {}
    )
    values = _plain_inputs(declared)
    guards = _guard_inputs(declared)
    for name, value in overrides.items():
        # An override is a value the caller wrote, so it takes the declared
        # type's form in a guard and the bare text everywhere else.
        kind = declared.get(name, (None, ""))[0]
        values[name] = value
        guards[name] = _as_expression(kind, value)
    context = f"job '{job_id}' calls '{called}'"
    substituted = {}
    for key, job in called_jobs.items():
        # The guard is substituted first, from the original, because the general
        # pass replaces `${{ inputs.x }}` with the plain text and a guard needs
        # the other form.
        job = dict(job) if isinstance(job, dict) else job
        if isinstance(job, dict) and isinstance(job.get("if"), str):
            job["if"] = _substitute_inputs(job["if"], guards, context)
        substituted[key] = _substitute_inputs(job, values, context)
    return (
        substituted,
        _string_map(data.get("env") if isinstance(data.get("env"), dict) else {}, f"{called}.env"),
    )


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _condition_of(raw_job: dict[str, Any]) -> str | None:
    """A job's `if:`, whether YAML read it as text or as a boolean.

    `if: false` and `if: true` are the two guards a repository writes as a bare
    boolean, and YAML hands them over as booleans rather than as the strings the
    rest of the key is. Dropping a non-string therefore dropped exactly the guard
    that says a job never runs, which is the one way to be wrong that costs a run
    on every push.
    """
    condition = raw_job.get("if")
    if isinstance(condition, bool):
        return "true" if condition else "false"
    return condition if isinstance(condition, str) else None


def _combined_condition(caller: str | None, called: str | None) -> str | None:
    """Both guards, or whichever one is there.

    The parameters are typed `str | None` because a YAML boolean is not text and
    must not reach here: the wide signature let one through, and `False` fell
    past every branch to no guard at all, which is the same defect as a dropped
    `if:` and one call site away from coming back.
    """
    if caller is not None and called is not None:
        return f"({caller}) && ({called})"
    return caller if caller is not None else called


def find_workflows(repo: Path) -> list[Workflow]:
    workflows_dir = repo / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return []
    workflows: list[Workflow] = []
    for path in sorted(workflows_dir.iterdir()):
        if is_workflow_file(path):
            workflows.append(parse_workflow(path, repo))
    return workflows


def parseable_workflows(repo: Path) -> tuple[list[Workflow], list[tuple[str, WorkflowError]]]:
    """The workflows that parse, and the files that do not.

    A malformed `release.yml` is no reason to refuse a repository's `ci.yml`,
    so a parse failure is collected rather than raised. It is reported only if
    nothing else matched, since otherwise the failure is in a file this job was
    never going to run and saying so would send a maintainer hunting it.
    """
    workflows: list[Workflow] = []
    broken: list[tuple[str, WorkflowError]] = []
    for path in workflow_files(repo):
        try:
            workflows.append(parse_workflow(path, repo))
        except WorkflowUnsupported:
            # A reusable call Cindral will not follow refuses the whole
            # repository, not just the file holding it: the call is expanded
            # while the file is read, so there is no separate "this file is
            # partly unreadable" to collect. That is deliberate, because
            # collecting it would make an unreadable call look like a malformed
            # file and answer it with a committed contract by accident, rather
            # than by the decision this exception is.
            raise
        except WorkflowError as exc:
            broken.append((path.name, exc))
    return workflows, broken


def workflow_files(repo: Path) -> list[Path]:
    workflows_dir = repo / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return []
    return [
        path
        for path in sorted(workflows_dir.iterdir())
        if is_workflow_file(path)
    ]


def is_workflow_file(path: Path) -> bool:
    return path.is_file() and path.suffix in {".yml", ".yaml"} and not path.name.startswith(".")


def _string_map(raw: dict[Any, Any], context: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            raise WorkflowError(f"{context} keys must be strings")
        result[k] = str(v)
    return result

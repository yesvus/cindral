"""Translating a parsed GitHub Actions workflow into a Cindral contract."""
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from .act import ActJob, act_contract, job_network
from .agent import JobSpec
from .contract import Contract
from .discovery import (
    UNMAPPED_JOB_KEYS,
    Workflow,
    WorkflowAmbiguous,
    WorkflowError,
    WorkflowJob,
    WorkflowNoMatch,
    WorkflowUnsupported,
    parseable_workflows,
    parse_workflow,
)
from .jobcondition import ConditionError, evaluate_condition
from .toolchain import is_setup_action, toolchain_for
from .triggers import event_declares, event_matches
from .workflow_context import ContextError, RunContext, event_name_for, resolve_services, run_context

__all__ = [
    "DEFAULT_RUNNER_IMAGE",
    "HOSTED_LABEL_SHAPE",
    "IMAGE_MAP",
    "UNMAPPED_JOB_KEYS",
    "Workflow",
    "WorkflowError",
    "WorkflowJob",
    "WorkflowNoMatch",
    "WorkflowUnsupported",
    "event_matches",
    "find_workflows",
    "is_workflow_file",
    "parse_workflow",
    "parseable_workflows",
    "resolve_workflow_contract",
    "resolve_workflow_job",
    "runner_image",
    "workflow_files",
    "workflow_to_contract",
]

from .discovery import find_workflows, is_workflow_file, workflow_files

DEFAULT_RUNNER_IMAGE = "ubuntu:24.04"
#: The same map keyed case-insensitively, because the hosted-label shape is matched
#: case-insensitively and `Ubuntu-22.04` must resolve the same way `ubuntu-22.04`
#: does. A shape check that is case-blind beside a lookup that is case-sensitive
#: refuses a label it has just agreed is one it knows.
_IMAGE_MAP_CI: dict[str, str] = {}

IMAGE_MAP: dict[str, str] = {
    "ubuntu-latest": "ubuntu:24.04",
    "ubuntu-24.04": "ubuntu:24.04",
    "ubuntu-22.04": "ubuntu:22.04",
    "ubuntu-20.04": "ubuntu:20.04",
}

_IMAGE_MAP_CI.update({key.lower(): value for key, value in IMAGE_MAP.items()})


def resolve_workflow_job(repo: Path, job: JobSpec) -> tuple[Workflow, WorkflowJob]:
    workflows, broken = parseable_workflows(repo)
    if not workflows:
        if broken:
            raise broken[0][1]
        raise WorkflowError("repository has no .cindral/ci.toml and no .github/workflows/*.{yml,yaml}")

    event_name = event_name_for(job)
    target_job = job.command[0] if job.command else None

    # A workflow that declares this event but filters this ref out is the
    # repository saying "not on this branch", which is a decision and not a gap.
    # Only a set where nothing declares the event at all is a fall-through.
    declares_event = any(
        event_declares(wf.on, event_name) for wf in workflows
    )

    matching: list[tuple[Workflow, WorkflowJob]] = []
    skipped: list[str] = []
    for wf in workflows:
        if not event_matches(wf.on, event_name, job.ref):
            continue
        for j_id, j in wf.jobs.items():
            if not _matches_target(j_id, j, wf, target_job):
                continue
            if not _job_runs(j, wf, job):
                skipped.append(f"{wf.path.name}:{j_id}")
                continue
            matching.append((wf, j))

    if not matching:
        target_clause = f" for target '{target_job}'" if target_job else ""
        if skipped:
            # the repository read the event and declined it, which is a decision
            # rather than a gap, so this is reported instead of falling through
            names = ", ".join(skipped)
            raise WorkflowError(
                f"every job matching event '{event_name}' is skipped by its own if: {names}{target_clause}"
            )
        if broken:
            names = ", ".join(name for name, _ in broken)
            detail = f", and these workflow files could not be parsed: {names}"
        else:
            detail = ""
        message = (
            f"no workflow job matched event '{event_name}' on ref '{job.ref}'{target_clause}{detail}"
        )
        # a named job that nothing defines is the caller being wrong, a file
        # that could not be read is a repository with a real mistake in it, and
        # a workflow that declares the event but excludes this ref is the
        # repository saying so on purpose; none of those is a fall-through
        if target_job is not None or broken or declares_event:
            raise WorkflowError(message)
        raise WorkflowNoMatch(message)

    if len(matching) > 1:
        if target_job is None:
            preferred = [m for m in matching if m[1].id in {"ci", "test"} or m[1].name.lower() in {"ci", "test"}]
            if len(preferred) == 1:
                return preferred[0]
        job_list = ", ".join(f"{wf.path.name}:{j.id}" for wf, j in matching)
        raise WorkflowAmbiguous(
            f"multiple workflow jobs matched ({job_list}); specify an unambiguous target job"
        )

    return matching[0]


def _matches_target(job_id: str, job: WorkflowJob, workflow: Workflow, target: str | None) -> bool:
    if target is None:
        return True
    # an inlined reusable-workflow job is addressed by the name it has inside
    # the workflow it came from, which is what a maintainer reads in a dispatch
    return target in {job_id, job.name, f"{workflow.path.name}:{job_id}"} or (
        "/" in job_id and job_id.rsplit("/", 1)[1] == target
    ) or (
        # the file's own name, when it names one job and only one
        target == workflow.path.name and len(workflow.jobs) == 1
    )


def _job_runs(job: WorkflowJob, workflow: Workflow, spec: JobSpec) -> bool:
    """Whether a job's own `if` lets it run for this event.

    A condition the run does not carry is a decision the repository has to make
    rather than one Cindral makes, so it is refused by the field it reads.
    """
    if job.condition is None:
        return True
    try:
        context = run_context(spec, workflow.name, job.id, job.env)
    except ContextError as exc:
        raise WorkflowUnsupported(str(exc)) from exc
    try:
        condition = evaluate_condition(job.condition, context)
    except ConditionError as exc:
        raise WorkflowUnsupported(
            f"job '{job.id}' has a job-level if that Cindral does not read: {exc}"
        ) from exc
    if condition.decided:
        return bool(condition.value)
    fields = ", ".join(f"'{name}'" for name in condition.unread)
    raise WorkflowUnsupported(
        f"job '{job.id}' has a job-level if that reads context {fields}, "
        "which this run does not carry; the repository must decide the condition"
    )


#: A hosted platform label's shape: a platform GitHub images, then `latest` or a
#: version. The *shape* and not a prefix, because a custom runner label may begin
#: with a platform name without naming a platform: `ubuntu-26.04` is an image this
#: build has no mapping for, and `ubuntu-builder` is a machine somebody registered.
HOSTED_LABEL_SHAPE = re.compile(r"^(ubuntu|windows|macos)-(latest|[0-9][0-9.]*)$", re.IGNORECASE)



def runner_image(job: WorkflowJob) -> str:
    """The image a runs-on label maps to, defaulting a self-hosted label.

    A self-hosted label names hardware rather than an image: `[self-hosted, Linux,
    ARM64, repo-label]` says where the job may run, and a device in the pool
    matching that label is exactly what it is asking for. Refusing it meant a
    repository whose runner is registered, online and idle could not use it, while
    the fallback image for the case was defined in this module and never used.

    The choice is visible in the image the contract carries and in the run log, so
    a job that landed somewhere other than its label says where.

    Only labels that name no platform get here. `self-hosted`, `Linux`, `ARM64` and
    a repository's own labels are hardware, and the pool has hardware — including a
    custom label that happens to start with a platform name, which is why the shape
    is matched rather than a prefix.

    There is no `self-hosted` special case. It was written and then measured to
    change no outcome, because the shape check already decides every case it did:
    a marker beside a custom label finds nothing shaped, and a marker beside a real
    image name should still be refused. One rule, no branch that only looks load-bearing. `ubuntu-*`,
    `windows-*` and `macos-*` name a platform image, so one that is not mapped is
    refused rather than answered with a near-enough substitute: `ubuntu-26.04`
    running on `ubuntu:24.04` is a toolchain the job did not ask for, and it would
    report success.
    """
    # Every label is read for a refusal before any map hit is accepted. A list is
    # "run it on whichever of these is free", so quietly taking the mapped one while
    # ignoring a Windows label beside it answers a different question than the job
    # asked, on a platform the author may have listed first.
    for label in job.runs_on:
        if HOSTED_LABEL_SHAPE.match(str(label)) and str(label).lower() not in _IMAGE_MAP_CI:
            raise WorkflowUnsupported(
                f"job '{job.id}' asks for '{label}', which this build has no image for;"
                " the device pool runs linux/amd64 and linux/arm64 only"
            )
    for label in job.runs_on:
        mapped = _IMAGE_MAP_CI.get(str(label).lower())
        if mapped is not None:
            return mapped
    return DEFAULT_RUNNER_IMAGE


def _is_checkout(step: dict[str, Any]) -> bool:
    uses = step.get("uses")
    return uses is not None and str(uses).split("@")[0] == "actions/checkout"


def _step_blocker(
    step: dict[str, Any], label: str, repo: Path | None = None, context: RunContext | None = None
) -> str | None:
    """The construct that stops native translation, or None for supported steps."""
    condition = step.get("if")
    if condition is not None and condition not in (True, "true", None):
        return "unsupported 'if' condition"
    if step.get("working-directory") is not None:
        return "unsupported 'working-directory'"
    if step.get("shell") is not None:
        return "step 'shell'"
    if step.get("continue-on-error") not in (None, False, "false") or step.get("timeout-minutes") is not None:
        return "step failure or timeout settings"
    uses = step.get("uses")
    if uses is not None and not _is_checkout(step):
        if is_setup_action(step):
            # the toolchain module owns which versions it can install, so it
            # answers rather than the blocker accepting the action by name
            try:
                toolchain_for(step, repo, _expander(context, label))
            except WorkflowUnsupported as exc:
                return str(exc)
        else:
            return f"unsupported action '{str(uses).strip()}'"
    if isinstance(step.get("env"), dict) and step["env"]:
        return "step 'env'"
    if uses is None and step.get("run") is None:
        return "neither 'run' nor 'uses'"
    return None


def _checkout_inputs(step: dict[str, Any]) -> list[str]:
    """The input names a checkout step sets, none of which either path applies."""
    inputs = step.get("with")
    if not _is_checkout(step) or not isinstance(inputs, dict) or not inputs:
        return []
    return [str(name) for name in sorted(inputs)]


def _step_values(step: dict[str, Any]) -> list[tuple[str, str]]:
    """The step strings the engine evaluates, each with the name it was read under."""
    values = [(key, value) for key in ("run", "if", "uses", "working-directory", "shell") if isinstance((value := step.get(key)), str)]
    for key in ("with", "env"):
        mapping = step.get(key)
        if isinstance(mapping, dict):
            values += [(f"{key}.{name}", item) for name, item in mapping.items() if isinstance(item, str)]
    return values


def step_label(job: WorkflowJob, index: int) -> str:
    return f"job '{job.id}' step {index}"


def _expander(context: RunContext | None, label: str):
    """Resolve a version input written as an expression, or refuse it by name.

    The refusal is the one every other step value gets, so an unreadable
    version is named the way the same field in a `run:` step would be.
    """
    if context is None:
        return None

    def expand(text: str, name: str) -> str:
        where = f"{label} 'with.{name}'"
        context.require_resolvable(text, where)
        return context.expand(text, where)

    return expand


def workflow_to_contract(job: WorkflowJob, context: RunContext, repo: Path | None = None) -> Contract:
    steps: list[str] = []
    for index, step in enumerate(job.steps, start=1):
        label = step_label(job, index)
        blocker = _step_blocker(step, label, repo, context)
        if blocker is not None:
            raise WorkflowUnsupported(
                f"{label} is not translatable natively: {blocker} (requires act engine)"
            )
        install = toolchain_for(step, repo, _expander(context, label))
        if install is not None:
            # the install lands where the action put the toolchain, so a later
            # step finds it on the PATH the container already has. The version
            # in it is already resolved, because the command names a release.
            steps += install.commands
            continue
        if "run" in step:
            run_cmd = str(step["run"]).strip()
            if run_cmd:
                steps.append(context.expand(run_cmd, f"{step_label(job, index)} 'run'"))

    if not steps:
        raise WorkflowError(f"job '{job.id}' has no executable steps")

    return Contract(
        steps=tuple(steps),
        image=runner_image(job),
        env=context.env,
        services=job.services,
        timeout_minutes=job.timeout_minutes,
        # The labels travel with the contract because an image is not the whole
        # request. `self-hosted` is dropped: every device in the pool is one, so
        # carrying it would assert nothing and would make the check below report a
        # device as mismatched when it is not.
        labels=tuple(
            str(label) for label in job.runs_on if str(label).strip().lower() != "self-hosted"
        ),
    )


def _native_blockers(job: WorkflowJob, repo: Path | None, context: RunContext) -> list[str]:
    """Every step that keeps a job off the native path, each naming its step."""
    return [
        f"{step_label(job, index)}: {reason}"
        for index, step in enumerate(job.steps, start=1)
        if (reason := _step_blocker(step, step_label(job, index), repo, context)) is not None
    ]


def resolve_workflow_contract(repo: Path, job: JobSpec) -> Contract:
    """Translate a supported workflow job or run it with act."""
    workflow, workflow_job = resolve_workflow_job(repo, job)
    if workflow_job.unmapped:
        key = workflow_job.unmapped[0]
        raise WorkflowUnsupported(
            f"job '{workflow_job.id}' declares {UNMAPPED_JOB_KEYS[key]} ('{key}'), which Cindral does not run yet"
        )
    try:
        context = run_context(job, workflow.name, workflow_job.id, workflow_job.env)
        blockers = _native_blockers(workflow_job, repo, context)
        native = not blockers
        services = resolve_services(context, workflow_job.services, native)
        for index, step in enumerate(workflow_job.steps, start=1):
            label = step_label(workflow_job, index)
            for name in _checkout_inputs(step):
                raise WorkflowUnsupported(
                    f"{label} sets checkout input '{name}', which Cindral does not apply"
                )
            for key, text in _step_values(step):
                check = context.require_expression if key == "if" else context.expand if native else context.require_resolvable
                check(text, f"{label} '{key}'")
        if native:
            return workflow_to_contract(replace(workflow_job, services=services), context, repo)
    except ContextError as exc:
        raise WorkflowUnsupported(str(exc)) from exc
    try:
        platforms = dict.fromkeys(workflow_job.runs_on, runner_image(workflow_job))
    except WorkflowUnsupported as exc:
        # act creates its own container, so a label with no image behind it stops
        # this path too. Both blockers are named: the label is rarely the only one.
        raise WorkflowUnsupported("; ".join([str(exc), *blockers]))
    return act_contract(
        ActJob(
            workflow_path=workflow.path,
            repo=repo,
            job_id=workflow_job.id,
            event_name=context.event_name,
            event=context.event,
            reason="; ".join(blockers),
            platforms=platforms,
            # every runs-on label maps to the same image, so act cannot fall back to a host run
            steps=workflow_job.steps,
            env=workflow_job.env,
            network=job_network(job.id),
            timeout_minutes=workflow_job.timeout_minutes,
        )
    )

"""Validate repository adapters and report runner-lane readiness."""
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import yaml

from .github import RepositoryRunner
from .policy import Policy


@dataclass(frozen=True)
class LaneReadiness:
    lane: str
    ready: bool
    required_labels: tuple[str, ...]
    registered: tuple[str, ...]
    online: tuple[str, ...]


def validate_adapter(path: str | Path, policy: Policy) -> tuple[str, ...]:
    adapter_path = Path(path)
    try:
        with adapter_path.open() as stream:
            workflow: Any = yaml.load(stream, Loader=yaml.BaseLoader)
    except (OSError, yaml.YAMLError) as exc:
        return (f"cannot read workflow adapter: {exc}",)
    if not isinstance(workflow, dict):
        return ("workflow adapter must contain a YAML mapping",)

    errors: list[str] = []
    triggers = workflow.get("on", {})
    dispatch = triggers.get("workflow_dispatch", {}) if isinstance(triggers, dict) else {}
    inputs = dispatch.get("inputs", {}) if isinstance(dispatch, dict) else {}
    if not isinstance(inputs, dict):
        inputs = {}
    for name in ("relay_lane", "relay_target", "relay_reason", "relay_ref"):
        if name not in inputs:
            errors.append(f"workflow_dispatch is missing the {name} input")

    lane_input = inputs.get("relay_lane", {})
    raw_options = lane_input.get("options", []) if isinstance(lane_input, dict) else []
    options = (
        set(raw_options)
        if isinstance(raw_options, list) and all(isinstance(item, str) for item in raw_options)
        else set()
    )
    expected_lanes = {"hosted", *policy.lane_labels}
    if not isinstance(lane_input, dict) or lane_input.get("type") != "choice" or lane_input.get("required") != "true":
        errors.append("relay_lane must be a required choice input")
    if options != expected_lanes:
        errors.append("relay_lane choices must match hosted and configured policy lanes")
    for name in ("relay_target", "relay_reason", "relay_ref"):
        if not isinstance(inputs.get(name), dict) or inputs[name].get("type") != "string":
            errors.append(f"{name} must be a string input")

    permissions = workflow.get("permissions", {})
    if not isinstance(permissions, dict) or permissions.get("contents") != "read":
        errors.append("workflow permissions must grant contents: read")

    concurrency = workflow.get("concurrency")
    if (
        not isinstance(concurrency, dict)
        or not concurrency.get("group")
        or concurrency.get("cancel-in-progress") != "false"
    ):
        errors.append("workflow concurrency must define a group and set cancel-in-progress to false")

    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        jobs = {}
    resolved_adapter = adapter_path.resolve()
    try:
        github_directory = resolved_adapter.parts.index(".github")
        repository_root = Path(*resolved_adapter.parts[:github_directory])
    except ValueError:
        repository_root = resolved_adapter.parent
    validated_reusable: set[Path] = set()
    for lane in expected_lanes:
        job = jobs.get(lane)
        if not isinstance(job, dict):
            errors.append(f"workflow is missing the {lane} job")
            continue
        condition = str(job.get("if", ""))
        if f"inputs.relay_lane == '{lane}'" not in condition:
            errors.append(f"{lane} job must be selected by relay_lane")
        expected_runner: tuple[str, ...]
        if lane == "hosted":
            expected_runner = policy.hosted_runner
        else:
            expected_runner = policy.lane_labels[lane]
            if lane == "device":
                expected_runner += ('${{ inputs.relay_target }}',)
                if "inputs.relay_target" not in condition:
                    errors.append("device job must validate relay_target")
        uses = job.get("uses")
        if isinstance(uses, str):
            _validate_reusable_call(job, lane, expected_runner, policy, errors)
            if uses.startswith("./"):
                reusable_path = repository_root / uses[2:]
                if reusable_path not in validated_reusable:
                    validated_reusable.add(reusable_path)
                    _validate_local_reusable(reusable_path, errors)
            continue

        if not _has_positive_timeout(job.get("timeout-minutes")):
            errors.append(f"{lane} job must define a positive timeout-minutes")
        runs_on = job.get("runs-on")
        if isinstance(runs_on, str):
            actual_runner = (runs_on,)
        elif isinstance(runs_on, list):
            actual_runner = tuple(str(label).strip('"\'') for label in runs_on)
        else:
            actual_runner = ()
        if actual_runner != expected_runner:
            errors.append(f"{lane} job runs-on must match the configured runner labels")

    device_condition = str(jobs.get("device", {}).get("if", "")) if isinstance(jobs.get("device"), dict) else ""
    configured_targets = set(policy.device_priority)
    declared_targets = set()
    for target in policy.device_priority:
        if f"inputs.relay_target == '{target}'" in device_condition:
            declared_targets.add(target)
    if declared_targets != configured_targets:
        errors.append("device job must allow exactly the configured device targets")
    return tuple(errors)


def _has_positive_timeout(value: Any) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def _validate_reusable_call(
    job: dict[str, Any],
    lane: str,
    expected_runner: tuple[str, ...],
    policy: Policy,
    errors: list[str],
) -> None:
    inputs = job.get("with", {})
    if not isinstance(inputs, dict) or inputs.get("lane") != lane:
        errors.append(f"{lane} reusable workflow must receive its lane input")
        return
    runner = inputs.get("runner")
    if lane == "device" and isinstance(runner, str):
        if not all(label in runner for label in policy.lane_labels["device"]):
            errors.append("device reusable workflow must receive the configured runner labels")
        if not all(target in runner for target in policy.device_priority):
            errors.append("device reusable workflow must map every configured relay_target")
    else:
        try:
            actual_runner = tuple(json.loads(runner)) if isinstance(runner, str) else ()
        except (json.JSONDecodeError, TypeError):
            actual_runner = ()
        if actual_runner != expected_runner:
            errors.append(f"{lane} reusable workflow must receive the configured runner labels")


def _validate_local_reusable(path: Path, errors: list[str]) -> None:
    try:
        with path.open() as stream:
            workflow: Any = yaml.load(stream, Loader=yaml.BaseLoader)
    except (OSError, yaml.YAMLError) as exc:
        errors.append(f"cannot read local reusable workflow {path}: {exc}")
        return
    if not isinstance(workflow, dict):
        errors.append(f"local reusable workflow {path} must contain a YAML mapping")
        return
    triggers = workflow.get("on", {})
    workflow_call = triggers.get("workflow_call", {}) if isinstance(triggers, dict) else {}
    inputs = workflow_call.get("inputs", {}) if isinstance(workflow_call, dict) else {}
    runner_input = inputs.get("runner") if isinstance(inputs, dict) else None
    if not isinstance(runner_input, dict) or runner_input.get("type") != "string":
        errors.append("local reusable workflow must accept a string runner input")
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict) or not jobs:
        errors.append("local reusable workflow must define at least one job")
        return
    for name, job in jobs.items():
        if not isinstance(job, dict):
            errors.append(f"local reusable job {name} must be a mapping")
            continue
        if not _has_positive_timeout(job.get("timeout-minutes")):
            errors.append(f"local reusable job {name} must define a positive timeout-minutes")
        if job.get("runs-on") != "${{ fromJSON(inputs.runner) }}":
            errors.append(f"local reusable job {name} must run on fromJSON(inputs.runner)")


def check_lane_readiness(
    policy: Policy,
    runners: tuple[RepositoryRunner, ...],
) -> tuple[LaneReadiness, ...]:
    readiness = [LaneReadiness("hosted", True, (), (), ())]
    for lane, labels in policy.lane_labels.items():
        targets = policy.device_priority if lane == "device" else (None,)
        for target in targets:
            required = set(labels)
            lane_name = lane
            if target is not None:
                required.add(target)
                lane_name = f"{lane}:{target}"
            matching = tuple(
                runner for runner in runners if required.issubset(set(runner.labels))
            )
            online = tuple(runner.name for runner in matching if runner.status == "online")
            readiness.append(
                LaneReadiness(
                    lane=lane_name,
                    ready=bool(online),
                    required_labels=tuple(sorted(required)),
                    registered=tuple(runner.name for runner in matching),
                    online=online,
                )
            )
    return tuple(readiness)

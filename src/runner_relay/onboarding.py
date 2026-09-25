"""Validate repository adapters and report runner-lane readiness."""
from dataclasses import dataclass
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
    try:
        with Path(path).open() as stream:
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
    for name in ("relay_lane", "relay_target", "relay_reason"):
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
    for name in ("relay_target", "relay_reason"):
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
    for lane in expected_lanes:
        job = jobs.get(lane)
        if not isinstance(job, dict):
            errors.append(f"workflow is missing the {lane} job")
            continue
        condition = str(job.get("if", ""))
        if f"inputs.relay_lane == '{lane}'" not in condition:
            errors.append(f"{lane} job must be selected by relay_lane")
        timeout = job.get("timeout-minutes")
        try:
            has_timeout = int(timeout) > 0
        except (TypeError, ValueError):
            has_timeout = False
        if not has_timeout:
            errors.append(f"{lane} job must define a positive timeout-minutes")

        expected_runner: tuple[str, ...]
        if lane == "hosted":
            expected_runner = policy.hosted_runner
        else:
            expected_runner = policy.lane_labels[lane]
            if lane == "device":
                expected_runner += ('${{ inputs.relay_target }}',)
                if "inputs.relay_target" not in condition:
                    errors.append("device job must validate relay_target")
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

"""The commit status text each run's verdict produces.

Kept apart from the HTTP handlers so the text a commit receives can be read and
tested without a request, and so the broker's own markers stay in one place.
"""
from collections.abc import Mapping, Sequence
from typing import Any
import re

from .github import MAX_STATUS_DESCRIPTION
from .jobrecord import Job
from .retry import MAX_ATTEMPTS, parked
from .steps import unfinished_step

# the device executor announces a step before running it and prefixes its own
# diagnostics with the same marker, so the last of either identifies where a
# failed run stopped
# A marker written by a current executor carries the run's nonce. The old form
# is still accepted so a status stays readable for a log an older device
# reported, but the reported step records are preferred when the agent sent
# them: they are the structured answer, and the log is a bounded tail.
STEP_MARKER = re.compile(r"^cindral: step (\d+)(?: [0-9a-f]{32})?: (.*)$", re.MULTILINE)
DIAGNOSTIC_MARKER = re.compile(r"^cindral: (?!step )(.*)$", re.MULTILINE)


def failure_description(
    exit_code: int, log: str, steps: Sequence[Mapping[str, Any]] = ()
) -> str:
    """Summarize a failed run for a commit status.

    The reported step records name the step the run stopped in, which is better
    than reading it back out of a log tail that may no longer contain the
    marker. On the records path a status carries only the step's declared
    command, never its output. The marker fallback reads whatever the log holds
    on a marker-shaped line, which a step can print; that is disclosed in
    docs/direct-execution.md rather than claimed to be impossible here.
    """
    summary = f"device pool run failed (exit {exit_code})"
    detail = _failure_detail(log, steps)
    return f"{summary}: {detail}"[:MAX_STATUS_DESCRIPTION] if detail else summary


def _failure_detail(log: str, steps: Sequence[Mapping[str, Any]] = ()) -> str:
    unfinished = unfinished_step(steps)
    if unfinished is not None:
        return f"step {unfinished['index']}: {str(unfinished.get('name', '')).strip()}"
    markers = STEP_MARKER.findall(log)
    if markers:
        index, command = markers[-1]
        return f"step {index}: {command.strip()}"
    # a run that died before the first step, in the checkout or the image pull,
    # leaves only an executor diagnostic behind
    diagnostics = DIAGNOSTIC_MARKER.findall(log)
    return diagnostics[-1].strip() if diagnostics else ""


def lost_description(job: Job, error: str) -> tuple[str, str]:
    """Status state and text for a run a device ended without reporting.

    Such a run used to reach the commit as `device pool run failed (exit 1)`,
    byte for byte what a failed test produces, so a maintainer could not tell the
    two apart or act on either. Naming the loss, its cause, and the attempt
    that follows is what makes the red check readable.
    """
    if parked(job.attempts):
        return "error", f"device pool run lost after {job.attempts} attempts: {error}"[:MAX_STATUS_DESCRIPTION]
    text = f"device pool run lost, retry {job.attempts + 1} of {MAX_ATTEMPTS}: {error}"
    # a device error is bounded at MAX_ERROR_CHARS, which is larger than a commit
    # status allows, so bound the text here the way failure_description does
    return "pending", text[:MAX_STATUS_DESCRIPTION]

"""Split a run's log into the per-step records the panel needs.

A device reports one log for the whole run, and the executor already announces
each step before running it. Reconstructing steps from that log keeps the wire
format unchanged, so an older agent's report still produces records.

Output is bounded per step and in total, because the stored log is bounded and
one step's output must not crowd out the others. A step that announced itself
and never finished is recorded as unfinished rather than failed, so a run killed
mid-step is not readable as a run whose steps all passed.
"""
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from dataclasses import dataclass, replace
import json
import re
import shlex

from .models import ExecutionResult

# The executor writes a fresh nonce into every marker it emits for one run, and
# a reader needs the same nonce to find them. A step's own output is the thing
# that matters here: a test printing a line shaped like a marker would otherwise
# split the run and invent a step that never ran. Guessing a 16-byte token per
# run is not something output can do.
NONCE_PATTERN = r"([0-9a-f]{32})"
STEP_MARKER = re.compile(rf"^cindral: step (\d+) {NONCE_PATTERN}: (.*)$", re.MULTILINE)
# a step that reached its end marker finished; a step that never reached it is
# the one the run stopped in
STEP_END = re.compile(rf"^cindral: step (\d+) {NONCE_PATTERN} end$", re.MULTILINE)
# a step whose output is bounded at the tail keeps the failure, which is what a
# reader opens the step for, rather than the install log that preceded it
MAX_STEP_CHARS = 16 * 1024
MAX_STEP_NAME_CHARS = 500
MAX_STEPS = 64
MAX_TOTAL_CHARS = 64 * 1024


@dataclass(frozen=True)
class Step:
    index: int
    name: str
    output: str
    completed: bool
    truncated: bool = False
    # steps that ran but are not in the set, recorded on the last kept step so a
    # bounded set is never mistaken for a complete one
    dropped: int = 0

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "output": self.output,
            "completed": self.completed,
            "truncated": self.truncated,
            "dropped": self.dropped,
        }


def _bound(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_STEP_CHARS:
        return text, False
    return text[-MAX_STEP_CHARS:], True


def parse_steps(log: str, nonce: str) -> list[Step]:
    """Read a run's log into steps, in the order the executor announced them.

    A run with no marker in its log announced no step, so it has none to report:
    it died in the checkout or the image pull, and the caller shows the broker
    diagnostics instead.
    """
    marks = _boundaries(log, nonce)
    if not marks:
        return []
    # a step with no end marker of its own ran until the next step began, which
    # is what a log written by an agent predating the end marker looks like
    finished = {int(index) for index, mark_nonce in STEP_END.findall(log) if mark_nonce == nonce}
    steps: list[Step] = []
    for position, (index, mark) in enumerate(marks):
        following = marks[position + 1][1].start() if position + 1 < len(marks) else len(log)
        body = log[mark.end() : following]
        end_marker = _end_marker(body, nonce)
        output_text = body[: end_marker.start()] if end_marker else body
        output, truncated = _bound(output_text)
        steps.append(
            Step(
                index=index,
                name=mark.group(3).strip(),
                output=output,
                completed=bool(end_marker) or (index in finished) or position + 1 < len(marks),
                truncated=truncated,
            )
        )
    return _within_budget(steps)


def _within_budget(steps: list[Step]) -> list[Step]:
    """Keep the record set small enough that a report body is accepted.

    The bound is applied here, where the body is built, not where it is
    validated: the broker refuses an oversized body before it looks at any field,
    so a total bounded only at validation time still lost the verdict on every
    retry. The last step is always kept even when the budget is spent, because
    that is the one a reader needs, and a set that lost steps says so rather than
    being shape-identical to a complete one.
    """
    # a step's name is repository-controlled and can be as long as a command
    # line, so it is bounded here rather than at validation, where the body has
    # already been built
    steps = [step if len(step.name) <= MAX_STEP_NAME_CHARS
             else replace(step, name=step.name[:MAX_STEP_NAME_CHARS], truncated=True)
             for step in steps]
    dropped = 0
    if len(steps) > MAX_STEPS:
        # the count bound drops steps too, and a set that lost them without
        # saying so is indistinguishable from a shorter run
        dropped = len(steps) - MAX_STEPS
        steps = [*steps[: MAX_STEPS - 1], steps[-1]]
    if len(steps) <= 1:
        return [replace(steps[0], dropped=steps[0].dropped + dropped)] if dropped else steps
    if sum(len(step.output) + len(step.name) for step in steps) <= MAX_TOTAL_CHARS:
        if not dropped:
            return steps
        # the count came off, so the last kept step carries what the set lost
        return [*steps[:-1], replace(steps[-1], dropped=steps[-1].dropped + dropped)]
    # the last step is the one the run stopped in, so its share of the budget is
    # reserved first and the earlier steps fill what is left
    final = steps[-1]
    kept: list[Step] = []
    spent = len(final.output) + len(final.name)
    for step in steps[:-1]:
        cost = len(step.output) + len(step.name)
        if spent + cost > MAX_TOTAL_CHARS:
            break
        spent += cost
        kept.append(step)
    kept.append(
        replace(final, output=final.output[:MAX_STEP_CHARS], truncated=final.truncated or len(final.output) > MAX_STEP_CHARS)
    )
    # added, not assigned: the count bound may already have dropped steps, and
    # overwriting their count here reported a total smaller than the set held
    dropped += len(steps) - len(kept)
    kept[-1] = replace(kept[-1], dropped=kept[-1].dropped + dropped)
    return kept


def _boundaries(log: str, nonce: str) -> list[tuple[int, re.Match[str]]]:
    """Markers that really are steps, in the order the executor announced them.

    A step whose output happens to print a line shaped like a marker would
    otherwise split a run in two and invent a step that never ran. The executor
    numbers steps from one and never repeats or goes backwards, so the first
    marker sets the sequence and every later marker has to be the next number.
    Anything else is output, and the output belongs to the step that printed it.
    """
    kept: list[tuple[int, re.Match[str]]] = []
    for mark in STEP_MARKER.finditer(log):
        if mark.group(2) != nonce:
            continue
        index = int(mark.group(1))
        if kept and index != kept[-1][0] + 1:
            continue
        kept.append((index, mark))
    return kept


def _end_marker(body: str, nonce: str) -> re.Match[str] | None:
    for match in STEP_END.finditer(body):
        if match.group(2) == nonce:
            return match
    return None


def _field(step: Step | Mapping[str, Any], name: str) -> Any:
    """Read a field from a parsed step or a reported one, whichever this is."""
    if isinstance(step, Mapping):
        return step.get(name)
    return getattr(step, name, None)


def unfinished_step(steps: Sequence[Step | Mapping[str, Any]]) -> Step | Mapping[str, Any] | None:
    """The step a failed run stopped in, or None when every step completed.

    The last step that did not complete is the one that failed, because the
    script stops at the first failing step. Reported step records are read
    rather than a log tail, since the records are the structured answer.
    """
    for step in reversed(steps):
        if not _field(step, "completed"):
            return step
    return None


def failed_step_index(log: str, steps: Sequence[Step | Mapping[str, Any]]) -> int | None:
    """The index of the step a failed run stopped in, or None."""
    step = unfinished_step(steps)
    index = _field(step, "index") if step is not None else None
    return index if isinstance(index, int) else None


def completed_log(
    log_path,
    exit_code: int,
    cleanup_warning: str | None,
    limit: Callable[[str], str],
    nonce: str,
    archive: str = "",
) -> ExecutionResult:
    """Read a finished run's log file into a result, with its steps.

    The cleanup warning is appended to the file before it is read, so the stored
    log and the reported steps describe the same text.
    """
    text = log_path.read_text(errors="replace") if log_path else ""
    warning = f"cindral: warning: {cleanup_warning}\n" if cleanup_warning else ""
    if warning and log_path is not None:
        with log_path.open("a") as log:
            log.write(warning)
    return result_for(exit_code, text + warning, log_limit=limit, nonce=nonce, archive=archive)


def run_script(steps: tuple[str, ...], nonce: str) -> str:
    """Build the shell script a run executes, one block per step.

    Each step prints an end marker only when it returns zero, so a step that
    reached its marker finished and one without it was cut short.

    The step runs in a subshell tested by `if` rather than relying on `set -e`:
    a step that runs `set +e` would otherwise disable the abort for every step
    after it and let a run with a failing command exit zero. As an `if`
    condition the group is also exempt from errexit, so the status tested is
    the step's own.
    """
    lines = ["set -e"]
    for index, step in enumerate(steps, start=1):
        lines.append(
            f"printf '\\ncindral: step {index} {nonce}: %s\\n' {shlex.quote(step)}\n"
            f"if ( {step}\n) ; then\n"
            f"  printf '\\ncindral: step {index} {nonce} end\\n'\n"
            "else\n"
            "  cindral_step_status=$?\n"
            "  exit \"$cindral_step_status\"\n"
            "fi"
        )
    return "\n".join(lines) + "\n"


def result_for(
    exit_code: int, log: str, log_limit: Callable[[str], str], nonce: str, archive: str = ""
) -> ExecutionResult:
    """Build the run's result, with steps taken from the device's full log.

    The reported log is bounded at the tail, so parsing that text would lose the
    steps that ran first. The complete log is the device's, and it is the only
    place the early step boundaries still exist.
    """
    return ExecutionResult(
        exit_code=exit_code,
        log=log_limit(log),
        steps=[step.as_dict() for step in parse_steps(log, nonce)],
        artifacts=archive,
    )


def _is_step(value: object) -> bool:
    """Whether a report entry carries the fields a step record needs."""
    if not isinstance(value, Mapping):
        return False
    index = value.get("index")
    return not isinstance(index, bool) and isinstance(index, int) and isinstance(value.get("name"), str)


def _reported_total(entries: Sequence[object]) -> int:
    """The number of steps a report describes, including ones it already dropped.

    Counted over the entries that are steps at all, because a report that
    carries entries the validator rejects describes fewer steps than it has
    lines. The stated count is taken from the last record that carries one,
    which is where a producer puts it.
    """
    valid = sum(1 for entry in entries if _is_step(entry))
    for entry in reversed(entries):
        if not _is_step(entry):
            continue
        stated = entry.get("dropped")
        if isinstance(stated, int) and not isinstance(stated, bool) and stated > 0:
            return valid + stated
    return valid


def encode_steps(value: object) -> list[dict]:
    """Validate steps a device reported before they are stored.

    A device is a trusted caller for its own runs, but a report is still
    untrusted input: a step record that is not a mapping, or a field of the
    wrong type, is dropped rather than stored, so a malformed report cannot put
    a shape on the job the reader would then have to defend against.
    """
    if not isinstance(value, list):
        return []
    steps: list[dict] = []
    total = 0
    # a report longer than the step cap loses records on this path as well as
    # on the budget path below, and a set that lost them without saying so is
    # what this whole bound exists to prevent
    # counted as the loop accepts them, not as they arrive: a report carrying
    # entries the loop rejected never ran those steps
    for entry in value[:MAX_STEPS]:
        if not _is_step(entry):
            continue
        index = entry["index"]
        name = entry["name"]
        output = entry.get("output")
        text, truncated = _bound(output if isinstance(output, str) else "")
        # bounded before the budget is charged, or the charge is for a length the
        # stored record does not have and records are dropped needlessly
        name = name[:MAX_STEP_NAME_CHARS]
        # the report body is capped at 1 MiB by the broker, so the whole record
        # set has to fit under the log budget. Without this a chatty run
        # produced a body the broker refused, and a real verdict became a
        # phantom "lost run" on every retry.
        if total + len(text) + len(name) > MAX_TOTAL_CHARS:
            # The record carrying the count is itself past the budget, so the
            # count moves onto the last one that is kept. It is assigned, not
            # added: the reported total already includes any count the report
            # stated, so adding the kept record's own would double it.
            if steps:
                steps[-1] = {**steps[-1], "dropped": max(0, _reported_total(value) - len(steps))}
            break
        total += len(text) + len(name)
        steps.append(
            {
                "index": index,
                "name": name[:MAX_STEP_NAME_CHARS],
                "output": text,
                "completed": bool(entry.get("completed")),
                "truncated": truncated or bool(entry.get("truncated")),
                "dropped": _stated_dropped(entry.get("dropped")),
            }
        )
    if len(value) > MAX_STEPS and steps:
        # the slice ended the loop rather than the budget, so nothing above
        # counted what it discarded
        steps[-1] = {**steps[-1], "dropped": max(0, _reported_total(value) - len(steps))}
    return steps


def _stated_dropped(value: object) -> int:
    """A reported dropped count, or zero. A negative count is nonsense."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def decode_steps(value: str | None) -> list[dict]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []

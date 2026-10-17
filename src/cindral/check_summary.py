"""What one device run renders as a GitHub check run: verdict, steps, and text.

A commit status is one line, so a failed run reaches the commit as a sentence
about the run and nothing about the step that broke. A check run carries the same
run as a named check with a markdown step table, a log panel, and an annotation
naming the failing step, which is what makes the failure readable from the commit
itself instead of from a panel.

Every field is derived from the stored job record, the state comes from the same
split the commit status is built on, and the conclusion is derived from the loss
the record kept rather than from the state, so the check run and the commit
status the broker still posts are two renderings of one verdict and a commit
cannot read as both green and red.

Nothing a device printed is published as it was stored. The log reaches the
commit only for a run that did not pass, and it is scrubbed of recognizable
credentials on the way, because a commit status is public and so is a check.
"""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .contract import CONTRACT_PATH
from .jobrecord import Job
from .jobs import FAILURE, SUCCESS
from .lease_reclaim import DEAD, PENDING, RUNNING
from .redact import redact, redacted_job, scrubbed
from .retry import LEASE_LOST, MAX_ATTEMPTS, parked
from .status_text import failure_description, lost_description
from .steps import MAX_STEP_CHARS

# the name GitHub lists the check under. It matches the commit status context on
# purpose: a branch that requires cindral/ci keeps passing on the context it
# already requires, so the two surfaces can be swapped without renaming anything
DEFAULT_CHECK_NAME = "cindral/ci"
# the sentences a run's commit status carries today, reused verbatim so the check
# run's title is the same sentence rather than a paraphrase that can drift
QUEUED_DESCRIPTION = "queued on the device pool"
RUNNING_DESCRIPTION = "device pool run in progress"
PASSED_DESCRIPTION = "device pool run passed"
# GitHub refuses an output field or an annotation past these sizes, and a request
# carrying one is rejected whole, so a long run would publish no check at all
MAX_OUTPUT_CHARS = 65535
MAX_ANNOTATION_TITLE = 255
MAX_ANNOTATION_DETAILS = 64 * 1024
# The executor stops a run at the limit the contract sets and reports this, and a
# service that never reports healthy leaves it the same way. It is the one exit
# code the broker owns rather than a step, which is what makes a timed-out run
# distinguishable from a change that broke.
TIMED_OUT_EXIT = 130


@dataclass(frozen=True)
class CheckVerdict:
    """One run as a check run: the commit status it agrees with, and its text."""

    state: str
    conclusion: str | None
    title: str
    summary: str
    text: str
    annotations: tuple[dict, ...]


def state_for(job: Job) -> str:
    """The commit status state this record produces, in post_status's vocabulary.

    A run no device finished is a pending retry until it is out of attempts and an
    error once it is, the same split lost_description makes, so the two surfaces
    read the same sentence about the same run.
    """
    if job.status == SUCCESS:
        return "success"
    if job.status == FAILURE:
        return "failure"
    if job.status == DEAD:
        return "error" if parked(job.attempts) else "pending"
    return "pending"


def conclusion_for(job: Job) -> str | None:
    """The check run conclusion, read off the loss rather than off the state.

    A commit status has no conclusion vocabulary, so `error` has to render as
    failure and a run the broker took away from its device reads on the commit as
    a change that broke. GitHub accepts `timed_out` and `cancelled` next to
    `failure`, and the three answer different questions: a step failed, the run
    hit its own limit, or the run was taken away and will not be retried. The
    distinction is what tells a maintainer whether to read the log or go look at
    the device.

    A run that is still waiting for another attempt has no conclusion, which is
    the only way the two surfaces can differ: the commit status says pending and
    the check says nothing yet.
    """
    if state_for(job) == "pending":
        return None
    if job.status == SUCCESS:
        return "success"
    if job.status == FAILURE:
        return "timed_out" if job.exit_code == TIMED_OUT_EXIT else "failure"
    # a run past its attempts is final, and what ended it is the loss the record
    # kept. A device that stopped answering is a run taken away, and a device
    # that answered with an error is one it could not start, which is a red the
    # maintainer has to fix either way.
    return "cancelled" if job.last_error == LEASE_LOST else "failure"


def title_for(job: Job) -> str:
    """The check run's title, which is the sentence the commit status also carries."""
    if job.status == SUCCESS:
        return PASSED_DESCRIPTION
    if job.status == FAILURE:
        return failure_description(job.exit_code if job.exit_code is not None else 1, job.log, job.steps)
    if job.status in {PENDING, RUNNING}:
        # a released run keeps the error from the attempt that lost it, so the
        # loss is read before the lane the run is waiting in
        if job.last_error:
            return lost_description(job, job.last_error)[1]
        return RUNNING_DESCRIPTION if job.status == RUNNING else QUEUED_DESCRIPTION
    return lost_description(job, job.last_error or "")[1]


def verdict_for(job: Job, *, annotation_path: str = CONTRACT_PATH) -> CheckVerdict:
    """Everything one run publishes as a check run, computed from its record.

    The record is scrubbed once here, so the summary, the title, and the
    annotation cannot disagree about the credentials they carry, and the log
    goes out only for a run that did not pass: a green check does not need the
    device's output on a public commit, and a run that passed is the one whose
    log most readers will never open.
    """
    state = state_for(job)
    conclusion = conclusion_for(job)
    record = redacted_job(job)
    return CheckVerdict(
        state=state,
        conclusion=conclusion,
        title=title_for(record),
        summary=summary_for(record),
        text=log_text(record) if conclusion not in {None, "success"} else "",
        annotations=annotations_for(record, path=annotation_path) if conclusion else (),
    )


def summary_for(job: Job) -> str:
    """The markdown the check run's panel opens with: the run, and every step."""
    steps = list(job.steps)
    lines = [_headline(job), "", _where(job), ""]
    if steps:
        lines.extend(_table(job, steps))
        lines.append("")
    else:
        lines.append(_no_steps(job))
    lines.extend(_disclosed(job, steps))
    return _bounded("\n".join(lines).rstrip() + "\n")


def log_text(job: Job) -> str:
    """The run's log tail, fenced so a commit renders it as text and not as prose.

    Fenced rather than written raw because output.text is markdown: an unfenced
    log collapses into a paragraph, and a log printing its own fence must not be
    able to close the block early.

    Bounded from the front, never from the back: the tail is the part a reader
    opened the check for, and a run that filled the log is the run whose failure
    is at the end of it. Scrubbed here rather than only in the verdict, so a
    caller that reaches for the log directly publishes no credential either.
    """
    log = redact(job.log or "")
    if not log:
        return ""
    notes: list[str] = []
    if scrubbed(job.log or ""):
        notes.append("cindral: credentials in this log were removed\n")
    if len(log) > MAX_OUTPUT_CHARS:
        # the store keeps 64 KiB of log and GitHub accepts one character less, so
        # a run that filled the log arrives one character too long
        notes.append("cindral: the beginning of this log is not shown\n")
        log = log[len(log) - (MAX_OUTPUT_CHARS - sum(len(note) for note in notes)) :]
    prefix = "".join(notes)
    fence = _fence(log)
    excess = len(prefix) + 2 * len(fence) + 2 + len(log) - MAX_OUTPUT_CHARS
    if excess > 0:
        log = log[excess:]
    return f"{prefix}{fence}\n{log.rstrip()}\n{fence}"


def annotations_for(job: Job, *, path: str = CONTRACT_PATH) -> tuple[dict, ...]:
    """The annotation naming what broke, once the run has a red conclusion.

    The Checks API requires a file path for an annotation, and a device run has
    no file of its own: the step that failed is declared in the repository's
    direct-execution contract, so the annotation is anchored there and carries
    the failing step's own output as its details. The message is the sentence the
    commit status carries, so the annotation and the status cannot disagree about
    what happened.

    Every field is scrubbed, because an annotation is rendered on the commit and
    on a diff, and the step output it carries is the run's most likely place for
    a credential the step interpolated into its own command.
    """
    if conclusion_for(job) in {None, "success"}:
        # a run that is queued, running, or waiting to be retried has nothing to
        # annotate: its commit is not red and the attempt is not over
        return ()
    step = _failed_step(job)
    title = f"cindral: step {step['index']} {_ended(job)}" if step is not None else "cindral: the run did not pass"
    details = str(step.get("output", "")) if step is not None else (job.log or job.last_error or "")
    return (
        {
            "path": path,
            "start_line": 1,
            "end_line": 1,
            "annotation_level": "failure",
            "title": title[:MAX_ANNOTATION_TITLE],
            "message": redact(title_for(job)),
            "raw_details": redact(details)[-MAX_ANNOTATION_DETAILS:],
        },
    )


def _ended(job: Job) -> str:
    """Whether the run's last step failed or was taken away from under it."""
    return "failed" if conclusion_for(job) == "failure" else "did not finish"


def _headline(job: Job) -> str:
    """The one line a reader sees before the table: what this run is doing."""
    step = _failed_step(job)
    if state_for(job) == "failure":
        if step is None:
            return "The run failed without a step record."
        return f"Step {step['index']} {_ended(job)}: `{_cell(step.get('name', ''))}`."
    if job.last_error:
        return f"The run did not finish: {job.last_error}"
    running = _running_step(job)
    if running is not None:
        return f"Step {running['index']} is running: `{_cell(running.get('name', ''))}`."
    return "Every step passed." if job.status == SUCCESS else "Waiting for a device."


def _tree(job: Job) -> str:
    """The tree this run built, and the head it was supposed to contain.

    The ref alone cannot answer "which tree is this verdict about": a merge ref is
    recomputed on every push to the branch, so resolving it after the fact can give
    a different answer than the run got. Naming the commit makes the decision
    checkable instead of trusted, and the head beside it is the whole question in a
    merge: whether the tree the run built contained the branch it came from.
    """
    parts = [f"built `{job.sha}`"]
    if job.status_sha and job.status_sha != job.sha:
        parts.append(f"head `{job.status_sha}`")
    return ", ".join(parts)


def _where(job: Job) -> str:
    """Which run this is: the ref, the device holding it, and how far it got."""
    where = [f"`{job.ref}`", _tree(job)]
    where.append(f"on `{job.device}`" if job.device else "not claimed yet")
    if job.attempts:
        where.append(f"attempt {job.attempts}")
    if job.steps:
        finished = sum(1 for step in job.steps if _completed(step))
        where.append(f"{finished} of {len(job.steps)} steps finished")
    return ", ".join(where)


def _table(job: Job, steps: Sequence[Mapping[str, Any]]) -> list[str]:
    """One row per step, in the order the run announced them, with its result.

    Every step is listed, including the ones that never ran: a reader who sees
    only the completed steps cannot tell a two-step run from a six-step run that
    stopped at two.
    """
    failed = _failed_step(job)
    running = _running_step(job)
    ended = _ended(job)
    rows = ["| # | Step | Result |", "| - | ---- | ------ |"]
    for step in steps:
        name = _cell(step.get("name", ""))
        result = _result(step, failed, running, ended)
        rows.append(f"| {step.get('index', '')} | `{name}` | {result} |")
    return rows


def _result(step: Mapping[str, Any], failed: Mapping[str, Any] | None, running: Mapping[str, Any] | None, ended: str) -> str:
    if _completed(step):
        return "passed"
    if failed is not None and _index(step) == _index(failed):
        return f"**{ended}**"
    if running is not None and _index(step) == _index(running):
        return "running"
    return "not finished"


def _no_steps(job: Job) -> str:
    if job.status == PENDING and not job.last_error:
        return "The run has not started yet: it is waiting for a device."
    if job.status == RUNNING:
        return "No step has started: a device has the run and has not announced one yet."
    return "No step ran."


def _disclosed(job: Job, steps: Sequence[Mapping[str, Any]]) -> list[str]:
    """What the record could not carry, said plainly rather than left to be inferred."""
    notes: list[str] = []
    dropped = sum(_count(step.get("dropped")) for step in steps)
    if dropped:
        notes.append(f"{dropped} earlier steps were not kept in this record.")
    if any(step.get("truncated") for step in steps):
        notes.append(f"Some step output was cut to its last {MAX_STEP_CHARS} characters.")
    if job.last_error:
        if parked(job.attempts):
            notes.append(f"Out of attempts after {MAX_ATTEMPTS}: this run will not be retried.")
        else:
            notes.append(f"Waiting for attempt {job.attempts + 1} of {MAX_ATTEMPTS}.")
    return notes


def _count(value: object) -> int:
    """A step record's dropped count, read back from stored JSON rather than trusted."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _failed_step(job: Job) -> Mapping[str, Any] | None:
    """The step a failed run stopped in, or None when it failed before one."""
    if state_for(job) != "failure":
        return None
    for step in reversed(list(job.steps)):
        if not _completed(step):
            return step
    return None


def _running_step(job: Job) -> Mapping[str, Any] | None:
    """The step a running job is in, or None when every step it has is done."""
    if job.status != RUNNING:
        return None
    for step in job.steps:
        if not _completed(step):
            return step
    return None


def _completed(step: Mapping[str, Any]) -> bool:
    return bool(step.get("completed"))


def _index(step: Mapping[str, Any]) -> object:
    return step.get("index")


def _cell(name: object) -> str:
    """A step command as a markdown table cell.

    A step's name is repository-controlled and can hold anything a shell accepts,
    so a pipe, a backtick, or a newline would break the table or the code span
    around it.
    """
    text = " ".join(str(name).split())
    return text.replace("`", "'").replace("|", "\\|")


def _fence(text: str) -> str:
    """A code fence longer than the longest run of backticks in the text it wraps."""
    longest = 0
    run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def _bounded(text: str) -> str:
    """Keep an output field inside the size GitHub accepts, saying what was cut."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    marker = "\nThe rest of this summary was cut to fit GitHub's size limit.\n"
    return text[: MAX_OUTPUT_CHARS - len(marker)].rstrip() + marker

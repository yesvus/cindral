"""Whether a device's own code is behind the broker it is taking work from.

A device agent is a checkout on the machine, not an image, so nothing in the
environment names the code it is running. When the agent's checkout is older
than the broker, it claims a job whose tree is at the current commit and
resolves that tree with code that predates it. The run then fails on something
the current code handles, and the report says the test failed rather than the
device was old.

Neither side can order the two commits alone: the broker holds no git history,
and the device cannot see the broker's commit until the broker tells it. So the
broker's build travels back on every heartbeat, the device names its own commit
on the way out, and the comparison happens here, where the history is.
"""
from pathlib import Path
import subprocess
import time
from typing import Callable, Mapping

from .build_info import git_prefix

UNKNOWN = "unknown"

#: a fetch is a courtesy, so it is bounded well below the time a device can sit
#: idle. A device that cannot fetch is stopped by the next line, not by this one.
FETCH_TIMEOUT_SECONDS = 20

#: what `git merge-base --is-ancestor` exits with to say "not an ancestor". Any
#: other nonzero status is a failure, and a failure is not an answer.
NOT_AN_ANCESTOR = 1

#: how long a device waits between fetch attempts. A device that cannot reach
#: its origin would otherwise spend every poll waiting on a fetch, which is
#: slower than the work it is refusing to take and hammers the remote besides.
FETCH_INTERVAL_SECONDS = 300.0


class Throttled:
    """Runs `work` at most once per interval, and remembers that it ran.

    Held by the device rather than kept inside the comparison, because the thing
    that decides how often to look is the loop that polls, and a comparison that
    carries its own retry state makes its answer depend on how often it was
    asked.
    """

    def __init__(
        self,
        work: Callable[..., None],
        interval: float = FETCH_INTERVAL_SECONDS,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._work = work
        self._interval = interval
        self._now = now
        self._last: float | None = None

    def __call__(self, *args: object) -> None:
        moment = self._now()
        if self._last is not None and moment - self._last < self._interval:
            return
        self._last = moment
        self._work(*args)


def checkout_root(start: str | Path | None = None) -> str | None:
    """The git checkout the running code lives in, or None when it is not one.

    Anchored on the package rather than the working directory, because an agent
    is started by a service manager from `/` and by hand from a home directory,
    and the comparison is only meaningful against the tree the code was loaded
    from.
    """
    here = Path(start) if start is not None else Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return None


def behind_reason(
    own_commit: str,
    broker_commit: str,
    source_root: str | Path | None,
    build: Mapping[str, str] | None = None,
    fetch: Callable[..., None] | None = None,
) -> str | None:
    """Why this device is behind the broker, or None when it is not.

    Ordering two commits needs the history between them. A device that is behind
    has exactly one signature: its commit is in the broker's history and not the
    other way round.

    A commit the device does not have at all is behind too, and that is the case
    a device hits every day. `git merge-base` answers "not an ancestor" for a
    commit it has never seen, which is indistinguishable from a real answer and
    would let exactly the device this guards proceed. So the object is looked up
    first, a fetch is attempted once so the reason can name a distance, and an
    object that is still absent is reported as what it is: a checkout that does
    not contain the code the broker is running.

    Unknown on either side means unknown, not behind. A refusal that fires on
    missing information takes every device offline the moment the broker stops
    naming itself, which is a larger outage than the one it prevents. The commit
    being absent is different: that is a fact about the checkout, not a gap in
    what the broker told us.
    """
    own = (own_commit or "").strip()
    broker = (broker_commit or "").strip()
    if not own or not broker or UNKNOWN in (own, broker):
        return None
    if own == broker:
        return None
    if source_root is None or not (Path(source_root) / ".git").exists():
        return None
    root = str(source_root)

    if not _has_object(root, broker) and fetch is not None:
        fetch(broker)
    if not _has_object(root, broker):
        return _reason(own, broker, build, missing=True)
    if _is_shallow(root):
        # a shallow clone answers "not an ancestor" for a commit on the far side
        # of the cut, which is the same false answer as never having fetched. The
        # distance still counts, so the device is stopped with a number rather
        # than let through, and the message says why the history is thin.
        return _reason(own, broker, build, distance=_count(root, f"{own}..{broker}"), shallow=True)

    # exit 0 means this device's commit is in the broker's history, so the device
    # is behind by exactly the commits between them. exit 1 is the opposite
    # answer, and it is the only other one that is an answer at all: a device
    # that has moved ahead of the broker, or diverged from it, and neither is
    # behind. Any other status is git failing, not git saying, and the two are
    # kept apart because a device is stopped on a fact and not on an error.
    relation = _git(root, "merge-base", "--is-ancestor", own, broker)
    if relation == 0:
        return _reason(own, broker, build, distance=_count(root, f"{own}..{broker}"))
    if relation == NOT_AN_ANCESTOR:
        return None
    # git could not answer. The commit is known to be present, so this is a
    # broken repository or a broken git rather than a device running old code,
    # and the executor will hit the same wall on the job and report it. Stopping
    # here would turn a broken clone into a device that silently goes quiet.
    return None


def _reason(
    own: str,
    broker: str,
    build: Mapping[str, str] | None,
    distance: int | None = None,
    missing: bool = False,
    shallow: bool = False,
) -> str:
    version = (build or {}).get("version") or "no version"
    how = f"{distance if distance is not None else 'an unknown number of'} commit(s) behind"
    if missing:
        how = "which this checkout does not have; run git fetch in the agent's checkout"
    elif shallow:
        how = f"{how} (the history here is shallow, so this is a lower bound)"
    return f"this device runs {own[:8]} {version} and the broker runs {broker[:8]}, {how}"


def _is_shallow(root: str) -> bool:
    """Whether the checkout holds only part of the history.

    The answer is in the output, not the exit status, and a failure to get it at
    all counts as shallow: git cannot order two commits across a gap it cannot
    see, and this is the one place where assuming the history is complete turns
    an unasked question into a device that claims.
    """
    answer = _output_text(root, "rev-parse", "--is-shallow-repository")
    return answer is None or answer.strip() != "false"


def _has_object(root: str, commit: str) -> bool:
    """Whether the checkout holds this commit at all.

    Separate from the ancestry test on purpose: a commit git cannot find is not
    the same answer as a commit that is not an ancestor, and conflating them is
    what lets an un-fetched device through.
    """
    return _git(root, "cat-file", "-e", f"{commit}^{{commit}}") == 0


def fetch_commit(root: str | Path, commit: str) -> None:
    """Best-effort one-object fetch, so the reason can name a distance.

    Failure is not interesting: an absent object is already a reason to stop, and
    a device on a network it cannot reach is not made worse by a fetch that did
    not happen. Deliberately not shallow, because a shallow fetch would leave
    the device unable to answer the next comparison.
    """
    _output(root, "fetch", "--quiet", "--no-tags", "origin", commit)


def _git(root: str | Path, *args: str) -> int | None:
    """The exit status of a git call, or None when it could not be made."""
    try:
        done = subprocess.run(
            ["git", *git_prefix(root), *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode


def _output_text(root: str | Path, *args: str) -> str | None:
    """A git call's standard output, or None when it could not be made."""
    try:
        done = subprocess.run(
            ["git", *git_prefix(root), *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _output(root: str | Path, *args: str) -> int | None:
    """The exit status of a git call whose output nobody needs."""
    try:
        done = subprocess.run(
            ["git", *git_prefix(root), *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=FETCH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode


def _count(root: str | Path, span: str) -> int | None:
    """Commits in `span`, or None when the count is unavailable."""
    try:
        done = subprocess.run(
            ["git", *git_prefix(root), "rev-list", "--count", span],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        return int(done.stdout.strip())
    except ValueError:
        return None

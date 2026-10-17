"""The check run client: open one run's check, then keep updating it as the run moves.

A commit status is written once per verdict and is gone; a check run is opened
when the run is queued and updated as it moves, which is what puts a step list, a
log, and a failing-step annotation on the commit. That needs the Checks API, which
only a GitHub App can use, and the ``checks: write`` permission on it.

Published from every place that knows a run's outcome, all through
``publish_check_run_quietly``, which absorbs a refusal so it is decided once: on
enqueue, on the verdict a device reports, and from both drains of a verdict the
queue owes, the request path and the reclaim timer.

The check run's id is kept per job and per name so every update lands on the one
check rather than opening a new one each time. A broker that restarts has no
memory of it, so the id is recovered from GitHub by the run's own external id
before a second check is ever opened for the same run.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import threading
import time
from typing import Any
import weakref
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .check_summary import DEFAULT_CHECK_NAME, CheckVerdict, verdict_for
from .contract import CONTRACT_PATH
from .github import GitHubAPIError, GitHubClient, is_repository_slug
from .jobrecord import Job, head_sha_for
from .lease_reclaim import RUNNING

# the publisher holding each client's open check runs, so the ids a job's check
# run has are the one record of which check that job owns
_PUBLISHERS: "weakref.WeakKeyDictionary[GitHubClient, CheckRunPublisher]" = weakref.WeakKeyDictionary()
_PUBLISHER_LOCK = threading.Lock()
# the listing is filtered by check name, so a page is the smallest read that can
# hold a run's own check
PER_PAGE = 100
# GitHub refuses a request past this many pages rather than answering with a
# prefix, and a prefix that stopped short of the run's own check is the duplicate
# the recovery exists to prevent
MAX_FIND_PAGES = 5
# a Retry-After this module cannot read is still a throttle, and waiting is the
# only correct reading of one
UNKNOWN_THROTTLE_SECONDS = 60.0


class CheckRunAPIError(GitHubAPIError):
    """A check run request GitHub refused, with the code it refused it with.

    The base error carries no code, so a throttled App and a missing permission
    are the same exception to a caller that only reads the message. They are not
    the same thing: one resolves itself after the wait GitHub names, the other
    does not resolve at all without the operator granting a permission.
    """

    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        """Whether another attempt can succeed with nothing changed but the wait."""
        return self.retry_after is not None or self.status_code is None


@dataclass(frozen=True)
class PublishedCheck:
    """The check run one job now owns."""

    check_run_id: int
    created: bool


def publish_check_run(
    client: GitHubClient,
    job: Job,
    *,
    name: str = DEFAULT_CHECK_NAME,
    target_url: str = "",
    annotation_path: str = CONTRACT_PATH,
    now: float | None = None,
) -> PublishedCheck:
    """Open `job`'s check run, or update the one it already has.

    This is the broker's one entry point for a run's check. The record decides
    what the run looks like: a job with no verdict yet is queued or in progress,
    and a job with one is completed with that conclusion, its step table, the log
    of a run that did not pass, and an annotation naming the step that failed.

    A check run is opened once per job and updated after that, so calling it again
    for a verdict already published does not open a second check.

    Raises `CheckRunAPIError` when GitHub refuses the request, which the call
    site absorbs: a broker without the App permission, or one GitHub is
    throttling, keeps posting the commit status it already posts, and that status
    is the surface a failed write is retried on. A throttled request carries the
    wait GitHub asked for, so the caller can come back after it rather than
    treating it as a permission it will never get.
    """
    return _publisher(client).publish(
        job, name=name, target_url=target_url, annotation_path=annotation_path, now=now
    )


def publish_check_run_quietly(
    client: GitHubClient | None, job: Job, *, name: str = DEFAULT_CHECK_NAME, target_url: str = ""
) -> bool:
    """Publish `job`'s check run, absorbing a refusal and reporting whether it landed.

    The commit status is the surface a failed write is retried on, so a check run
    GitHub refuses must not take the run's outcome with it. Absorbing the refusal
    here rather than at each call site is what stops it being re-decided per caller.
    """
    if client is None:
        return False
    try:
        publish_check_run(client, job, name=name, target_url=target_url)
    except Exception:  # noqa: BLE001 - the name says it absorbs a refusal
        return False
    return True


def iso8601(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CheckRunClient:
    """The three Checks API calls one run needs: open, update, and find.

    Kept beside the publisher rather than on GitHubClient because the check run
    lifecycle is one concern with state between calls, where the commit status
    client posts a single verdict and holds none.
    """

    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    def create(self, repository: str, payload: Mapping[str, Any]) -> int:
        body = self._send("POST", self._path(repository, "check-runs"), payload)
        identifier = body.get("id")
        if not isinstance(identifier, int) or isinstance(identifier, bool):
            raise GitHubAPIError("GitHub created a check run without an id")
        return identifier

    def update(self, repository: str, check_run_id: int, payload: Mapping[str, Any]) -> None:
        self._send("PATCH", self._path(repository, f"check-runs/{check_run_id}"), payload)

    def find(self, repository: str, sha: str, name: str, external_id: str) -> tuple[int, bool] | None:
        """The check run a job already owns, and whether GitHub has finished it.

        A broker restart is the only way to reach this: without the id in memory
        the next update would open a second check for the same run and leave the
        first one pending forever. The external id is the job's own, so two runs
        of one commit, and one job run twice, are told apart.

        Paged, because a commit the same job is retried on holds one check per
        attempt under that name, and a run's own check past the first page is one
        this cannot see: it would open a second check and leave the first pending
        on the commit forever, which is the outcome the recovery exists to
        prevent. A full page at the cap raises rather than answering with a prefix.
        """
        for page in range(1, MAX_FIND_PAGES + 1):
            query = f"?check_name={quote(name, safe='')}&per_page={PER_PAGE}&page={page}"
            path = self._path(repository, f"commits/{quote(sha, safe='')}/check-runs") + query
            runs = self._runs(self._send("GET", path, None))
            for entry in runs:
                if not isinstance(entry, dict) or entry.get("external_id") != external_id:
                    continue
                identifier = entry.get("id")
                if not isinstance(identifier, int) or isinstance(identifier, bool):
                    raise GitHubAPIError("GitHub returned a check run without a numeric id")
                return identifier, entry.get("status") == "completed"
            if len(runs) < PER_PAGE:
                return None
        raise GitHubAPIError(
            f"GitHub returned more than {MAX_FIND_PAGES * PER_PAGE} check runs named {name} on {sha[:7]}"
        )

    def _runs(self, body: Mapping[str, Any]) -> list[Any]:
        runs = body.get("check_runs")
        if not isinstance(runs, list):
            raise GitHubAPIError("GitHub returned an invalid check run list")
        return runs

    def _path(self, repository: str, suffix: str) -> str:
        if not is_repository_slug(repository):
            raise ValueError("repository must use the owner/name format")
        return f"/repos/{quote(repository, safe='/')}/{suffix}"

    def _send(self, method: str, path: str, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        request = Request(
            f"{self._client.api_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._client._credential()}",
                **({"Content-Type": "application/json"} if payload is not None else {}),
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method=method,
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise CheckRunAPIError(
                f"GitHub check run request failed with HTTP {exc.code}: {detail}",
                exc.code,
                _retry_after(exc),
            ) from exc
        except URLError as exc:
            # no code at all: the request never reached GitHub, and the same
            # request is worth another attempt
            raise CheckRunAPIError(f"GitHub check run request failed: {exc.reason}") from exc
        if not raw:
            return {}
        try:
            body: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GitHubAPIError("GitHub returned an invalid check run response") from exc
        if not isinstance(body, dict):
            raise GitHubAPIError("GitHub returned an invalid check run response")
        return body


class CheckRunPublisher:
    """Publishes one job's check run, opening it the first time and updating it after.

    The check run ids it holds are the only record of which check a job owns:
    without them an update would open a second check for the same run and leave
    the first one pending on the commit forever. A broker restart loses them, so
    the id is recovered from GitHub by the run's external id before opening one,
    and an id GitHub no longer has is dropped rather than updated again. They are
    keyed by the check name as well as the job, because an operator who renames
    the status context gets a differently named check, and reusing one id across
    the two would leave the check under the old name pending forever. The id is
    read and written outside the lock, because holding it across two requests
    would serialize every publish the broker makes; two requests for one job at
    the same moment can still open two checks, and the later one is the one
    GitHub shows.
    """

    def __init__(self, client: GitHubClient) -> None:
        self._checks = CheckRunClient(client)
        self._open: dict[tuple[str, str, str], tuple[int, bool]] = {}
        self._lock = threading.Lock()

    def publish(
        self,
        job: Job,
        *,
        name: str = DEFAULT_CHECK_NAME,
        target_url: str = "",
        annotation_path: str = CONTRACT_PATH,
        now: float | None = None,
    ) -> PublishedCheck:
        if not is_repository_slug(job.repository):
            raise ValueError("repository must use the owner/name format")
        sha = head_sha_for(job.sha, job.status_sha)
        if not sha:
            raise ValueError("a check run requires a commit sha")
        verdict = verdict_for(job, annotation_path=annotation_path)
        # the lookup is outside the recovery, so a 404 from it is a commit the
        # credential cannot read rather than a check run that is gone, and
        # answering it the same way twice would be a loop
        held = self._held(job, sha, name)
        # A completed check is the later truth: a job the queue released for
        # another device is pending, and writing that reopens a finished check.
        if held is not None and held[1] and verdict.conclusion is None:
            return PublishedCheck(check_run_id=held[0], created=False)
        try:
            return self._write(job, sha, name, held, verdict, target_url, now)
        except CheckRunAPIError as exc:
            if exc.status_code != 404:
                raise
        # A check run GitHub no longer has is one a retention policy or a
        # maintainer removed, and the cached id would 404 every later update for
        # the rest of the broker's life. It is dropped, the run is looked up again
        # under the same name, and the write is made once more, which reopens the
        # check when GitHub has no record of it either. A 404 on that second
        # attempt is a repository GitHub will not serve, and is raised.
        with self._lock:
            self._open.pop((job.repository, job.id, name), None)
        return self._write(job, sha, name, self._held(job, sha, name), verdict, target_url, now)

    def _write(
        self,
        job: Job,
        sha: str,
        name: str,
        held: tuple[int, bool] | None,
        verdict: CheckVerdict,
        target_url: str,
        now: float | None,
    ) -> PublishedCheck:
        """Send one check run body, opening the check run or updating the one held."""
        # An annotation goes only with the update that finishes a run: GitHub
        # appends them on every update, and the broker republishes a verdict
        # whenever one it owed reaches the commit late.
        annotations = () if held is not None and held[1] else verdict.annotations
        payload = _payload(job, verdict, name=name, target_url=target_url, now=now, annotations=annotations)
        if held is None:
            body = {**payload, "head_sha": sha, "started_at": iso8601(job.created_at)}
            check_run_id = self._checks.create(job.repository, body)
            created = True
        else:
            check_run_id = held[0]
            self._checks.update(job.repository, check_run_id, payload)
            created = False
        # Written only once the request above has returned, so a refused publish
        # leaves the run looking unfinished and the retry sends the annotation
        # again. A request GitHub applied and then failed to answer is the one
        # case that cannot be told apart from one it never saw, and it costs a
        # repeated annotation on an already red commit.
        with self._lock:
            self._open[(job.repository, job.id, name)] = (check_run_id, verdict.conclusion is not None)
        return PublishedCheck(check_run_id=check_run_id, created=created)

    def _held(self, job: Job, sha: str, name: str) -> tuple[int, bool] | None:
        """The check run this job already owns: in memory first, then on GitHub."""
        key = (job.repository, job.id, name)
        with self._lock:
            held = self._open.get(key)
        if held is not None:
            return held
        found = self._checks.find(job.repository, sha, name, job.id)
        if found is not None:
            with self._lock:
                self._open[key] = found
        return found


def _publisher(client: GitHubClient) -> CheckRunPublisher:
    """The publisher holding this client's open check runs, one per client.

    The broker keeps a single GitHub client for its life, so this is one
    publisher for the process. It is looked up rather than built per call because
    the ids it holds are the only record of which check run a job owns.
    """
    with _PUBLISHER_LOCK:
        publisher = _PUBLISHERS.get(client)
        if publisher is None:
            publisher = CheckRunPublisher(client)
            _PUBLISHERS[client] = publisher
        return publisher


def _retry_after(exc: HTTPError) -> float | None:
    """The wait GitHub asked for before another attempt, or None when it asked for none.

    GitHub sends a count of seconds. A header this cannot read is still a
    throttle rather than a permission the App will never be granted, so it falls
    back to a wait instead of reading as one.
    """
    raw = exc.headers.get("Retry-After") if exc.headers else None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return UNKNOWN_THROTTLE_SECONDS


def _payload(
    job: Job,
    verdict: CheckVerdict,
    *,
    name: str,
    target_url: str,
    now: float | None,
    annotations: tuple[dict, ...],
) -> dict[str, Any]:
    """One check run body, built from the record's verdict."""
    output: dict[str, Any] = {
        "title": verdict.title,
        "summary": verdict.summary,
        "text": verdict.text,
    }
    if annotations:
        output["annotations"] = list(annotations)
    payload: dict[str, Any] = {"name": name, "external_id": job.id, "output": output}
    if target_url:
        payload["details_url"] = target_url
    if verdict.conclusion is None:
        # a run that has not been claimed yet is waiting in the queue; one a
        # device holds is in progress, and the difference is what a reader of the
        # commit sees while the run is quiet
        payload["status"] = "in_progress" if job.status == RUNNING else "queued"
        return payload
    payload["status"] = "completed"
    payload["conclusion"] = verdict.conclusion
    payload["completed_at"] = iso8601(time.time() if now is None else now)
    return payload

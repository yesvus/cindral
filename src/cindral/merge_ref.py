"""Which commit a pull-request run should actually build.

Its own module because this is the question a check run's verdict depends on, and
it is asked once per pull-request run. GitHub computes a pull request's test
merge commit asynchronously, so a `synchronize` event can be delivered while the
merge ref still points at the previous head. The event's `merge_commit_sha` is
then a perfectly real sha, for a commit that does not contain the code under
review, and a job built from it produces a verdict about something else. The
failure is invisible: the run logs a job, a sha and a plausible test count, and
the check is attached to a different commit from the one it describes.

So the ref is resolved at dispatch time, and the resolved commit is only used when
its parents include the head the event arrived with. That is the whole test: a
merge commit for this head has this head as a parent, and one left over from the
previous push does not.
"""
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import GitHubAPIError

__all__ = ["is_placeholder_sha", "pull_request_merge_sha", "run_sha_for_pull_request"]

#: a merge ref that is absent, or names a commit that cannot be produced because
#: the pull request conflicts or its base moved
UNMERGEABLE = (404, 409, 422)


def pull_request_merge_sha(
    api_url: str, headers: dict[str, str], repository: str, number: int
) -> tuple[str, tuple[str, ...]] | None:
    """The pull request's test-merge commit and its parents, or None if unmergeable.

    One request rather than two: the commit endpoint answers with the sha and the
    parents together, so verifying the merge is current costs no more than
    resolving it.

    `repository` must already be a validated slug and is interpolated into the
    path as given; the client that calls this owns that check, which is where every
    other repository name in the broker is checked.

    A body that is not a commit raises rather than returning None. "No merge" and
    "the lookup is broken" must not look the same to the caller: returning None for
    both means every pull request quietly stops testing the merge when the lookup
    breaks, and nothing says so. Raising puts it on the path that already logs and
    falls back to the head, which is the same runnable outcome with a cause.
    """
    if number <= 0:
        raise ValueError("pull request number must be positive")
    url = f"{api_url}/repos/{repository}/commits/refs/pull/{number}/merge"
    try:
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            commit = json.loads(response.read() or b"{}")
    except HTTPError as exc:
        if exc.code in UNMERGEABLE:
            return None
        detail = exc.read().decode(errors="replace")
        raise GitHubAPIError(
            f"GitHub merge ref lookup failed with HTTP {exc.code}: {detail}"
        ) from exc
    except URLError as exc:
        raise GitHubAPIError(f"GitHub merge ref lookup failed: {exc.reason}") from exc
    if not isinstance(commit, dict):
        raise GitHubAPIError("GitHub merge ref lookup returned something that is not a commit")
    sha = commit.get("sha")
    parents = commit.get("parents")
    if not isinstance(sha, str) or not sha or not isinstance(parents, list):
        raise GitHubAPIError("GitHub merge ref lookup returned a commit with no sha or no parents")
    return sha, tuple(
        str(parent["sha"])
        for parent in parents
        if isinstance(parent, dict) and parent.get("sha")
    )


def is_placeholder_sha(sha: str) -> bool:
    """Whether a sha is GitHub's "there is no such commit" answer.

    `merge_commit_sha` is forty zeros when a pull request has no merge commit yet,
    or when it conflicts and there never will be. It is a string, so a truthiness
    test walks straight past it, and the failure is silent: the run is enqueued
    against a commit that cannot be checked out while the verdict is attached to the
    real head, so the check reports on code nothing ever built.

    Only the placeholder is rejected here, not an oddly shaped sha. This reads a
    value from someone else's payload, and refusing a dispatch over a format we
    have not seen before fails a run that would otherwise work. A sha that came
    back from the API is ours to check, and that is done where it is used.
    """
    return bool(sha) and set(sha) <= {"0"}


def run_sha_for_pull_request(
    head_sha: str, resolved: tuple[str, tuple[str, ...]] | None
) -> str:
    """The commit to build, which is the event's merge only when it is current.

    A merge whose parents include the head is this pull request's test merge, and
    testing the merge is what a pull request check is for. Anything else, including
    no merge at all, falls back to the head: the code as written rather than as
    merged, which is the honest thing to build when there is no merge to test.
    """
    sha, parents = _as_merge(resolved)
    if sha is None or not _names_a_commit(sha) or head_sha not in parents:
        return head_sha
    return sha


def _as_merge(resolved: object) -> tuple[str | None, tuple[str, ...]]:
    """Read a merge answer, or nothing at all if it is not one.

    Total on purpose. This is the last thing between a lookup and the commit a run
    is built from, so anything that is not exactly the shape asked for is treated as
    no merge rather than allowed to raise, and the head is used. A caller that
    cannot answer has not earned the right to stop a run.
    """
    if not isinstance(resolved, tuple) or len(resolved) != 2:
        return None, ()
    sha, parents = resolved
    if not isinstance(sha, str) or not isinstance(parents, (list, tuple)):
        return None, ()
    return sha, tuple(parent for parent in parents if isinstance(parent, str))


def _names_a_commit(sha: str) -> bool:
    """Whether a sha from the API is a full git object name.

    A response we asked for, so a malformed one is a fault worth falling back from
    rather than a value worth building. The head is always used instead, which
    always exists.
    """
    if is_placeholder_sha(sha):
        return False
    return len(sha) == 40 and all(character in "0123456789abcdef" for character in sha.lower())

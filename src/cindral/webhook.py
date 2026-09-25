"""GitHub webhook verification and event parsing.

Kept separate from the HTTP handler so the cryptography and the payload shape
can be tested without binding a socket.
"""
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"
DELIVERY_HEADER = "X-GitHub-Delivery"


class WebhookError(Exception):
    """Raised when a delivery must be refused."""


@dataclass(frozen=True)
class PushEvent:
    repository: str
    private: bool
    ref: str
    after: str
    default_branch: str

    @property
    def branch(self) -> str:
        # push events for a branch carry refs/heads/<branch>; tags and
        # refs/keep-alive style refs are not dispatchable
        prefix = "refs/heads/"
        return self.ref[len(prefix):] if self.ref.startswith(prefix) else ""


@dataclass(frozen=True)
class PullRequestEvent:
    repository: str
    private: bool
    default_branch: str
    number: int
    action: str
    author_association: str
    head_repo: str = ""
    head_sha: str = ""
    merge_sha: str = ""
    draft: bool = False

    @property
    def ref(self) -> str:
        return f"refs/pull/{self.number}/merge"

    @property
    def same_repo(self) -> bool:
        return bool(self.head_repo) and self.head_repo == self.repository

    @property
    def trusted(self) -> bool:
        return self.author_association in {"OWNER", "MEMBER", "COLLABORATOR"}

    @property
    def should_route(self) -> bool:
        return self.action in {"opened", "reopened", "synchronize", "ready_for_review"}

    @property
    def run_sha(self) -> str:
        # the merge commit is what a pull_request check should test; fall back
        # to the head commit when GitHub has not produced a merge commit yet
        return self.merge_sha or self.head_sha

    @property
    def status_sha(self) -> str:
        # statuses must land on the head commit so they surface on the PR
        return self.head_sha or self.run_sha

    @property
    def direct_eligible(self) -> bool:
        # only same-repository, trusted, non-draft pull requests that are routed
        # may run on the device pool; fork code never executes there
        return (
            self.should_route
            and self.same_repo
            and self.trusted
            and not self.draft
            and bool(self.run_sha)
        )


def sign(secret: str, body: bytes) -> str:
    """The value GitHub sends in X-Hub-Signature-256 for this body."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header:
        return False
    # constant-time compare: a length or prefix oracle here would let an
    # attacker recover the digest byte by byte
    return hmac.compare_digest(sign(secret, body), header.strip())


def parse_push_event(body: bytes) -> PushEvent:
    try:
        payload: Any = json.loads(body)
    except json.JSONDecodeError as exc:
        raise WebhookError("body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise WebhookError("body is not a JSON object")
    repository = payload.get("repository")
    if not isinstance(repository, dict) or not isinstance(repository.get("full_name"), str):
        raise WebhookError("payload has no repository.full_name")
    default_branch = repository.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise WebhookError("payload has no repository.default_branch")
    ref = payload.get("ref")
    after = payload.get("after")
    if not isinstance(ref, str) or not isinstance(after, str):
        raise WebhookError("payload has no ref or after")
    deleted = payload.get("deleted")
    if deleted is True:
        raise WebhookError("ref was deleted")
    return PushEvent(
        repository=repository["full_name"],
        private=bool(repository.get("private")),
        ref=ref,
        after=after,
        default_branch=default_branch,
    )


def parse_pull_request_event(body: bytes) -> PullRequestEvent:
    try:
        payload: Any = json.loads(body)
    except json.JSONDecodeError as exc:
        raise WebhookError("body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise WebhookError("body is not a JSON object")
    repository = payload.get("repository")
    if not isinstance(repository, dict) or not isinstance(repository.get("full_name"), str):
        raise WebhookError("payload has no repository.full_name")
    default_branch = repository.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise WebhookError("payload has no repository.default_branch")
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        raise WebhookError("payload has no pull_request")
    number = payload.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise WebhookError("payload has an invalid pull request number")
    action = payload.get("action")
    author_association = pull_request.get("author_association")
    if not isinstance(action, str) or not isinstance(author_association, str):
        raise WebhookError("payload has no pull request action or author association")
    head = pull_request.get("head")
    head_repo = ""
    head_sha = ""
    if isinstance(head, dict):
        head_sha_value = head.get("sha")
        head_sha = head_sha_value if isinstance(head_sha_value, str) else ""
        head_repo_value = head.get("repo")
        if isinstance(head_repo_value, dict) and isinstance(head_repo_value.get("full_name"), str):
            head_repo = head_repo_value["full_name"]
    merge_sha_value = pull_request.get("merge_commit_sha")
    merge_sha = merge_sha_value if isinstance(merge_sha_value, str) else ""
    return PullRequestEvent(
        repository=repository["full_name"],
        private=bool(repository.get("private")),
        default_branch=default_branch,
        number=number,
        action=action,
        author_association=author_association,
        head_repo=head_repo,
        head_sha=head_sha,
        merge_sha=merge_sha,
        draft=bool(pull_request.get("draft")),
    )

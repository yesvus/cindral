"""GitHub webhook verification and push-event parsing.

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

    @property
    def branch(self) -> str:
        # push events for a branch carry refs/heads/<branch>; tags and
        # refs/keep-alive style refs are not dispatchable
        prefix = "refs/heads/"
        return self.ref[len(prefix):] if self.ref.startswith(prefix) else ""


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
    )

"""Which device a credential speaks for, so a device name is not just a claim.

Every device-facing request names its device, in the body for claim, renew,
report and heartbeat, and in a header for an artifact upload. Under one shared
agent token that name was a claim anyone could make: naming the device holding a
lease was enough to report its verdict and write the commit status, enough to
claim work and take it from the pool, and enough to announce labels and
concurrency the device does not have.

So the credential has to say which device is speaking, rather than only that
someone is. A per-device token map does that, and it needs no agent change: a
device presents whatever is in its token environment variable, so a device given
its own value needs no redeploy.

With no map configured nothing here binds a device, and the shared token stands
in. That is the rollout path, not a resting state: a shared credential that still
forges means the hole is open while the configuration reads as closed.
"""
from __future__ import annotations

import hashlib
import json
import hmac
from collections.abc import Mapping
from typing import Any

_INDEXES: dict[int, tuple[Mapping[str, str], dict[bytes, str]]] = {}


def parse(raw: str | None) -> dict[str, str] | None:
    """A device-to-token map from JSON, or None when none is configured.

    JSON rather than a positional convention, because the key is the thing that
    matters. A format readable as "the first token is the first device" would put
    the device's identity back into a guess, which is the thing being removed.
    """
    if not raw or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "CINDRAL_AGENT_TOKENS must be a JSON object of device to token"
        ) from exc
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError("CINDRAL_AGENT_TOKENS must be a JSON object of device to token")
    tokens = {str(name): str(value) for name, value in parsed.items()}
    if any(not name or not value for name, value in tokens.items()):
        raise ValueError("every device in CINDRAL_AGENT_TOKENS needs a name and a token")
    return tokens


def index(tokens: Mapping[str, str]) -> dict[bytes, str]:
    """Digest-keyed device names, so a presented token is looked up and not compared.

    Comparing each configured token in turn would leak, through timing, how far a
    guess matched. Keyed by digest the read is one dictionary lookup whatever the
    guess is.

    Cached against the map's identity: rebuilding it per request would cost a hash
    per device on every heartbeat, and a device map is fixed for the process's life.
    The value keeps a reference to the map it was built from, so an id that has
    been reused by a different map cannot return the wrong names.
    """
    cached = _INDEXES.get(id(tokens))
    if cached is not None and cached[0] is tokens:
        return cached[1]
    built = {hashlib.sha256(token.encode()).digest(): name for name, token in tokens.items()}
    if len(built) != len(tokens):
        # two devices on one token makes the map ambiguous, and the loser would
        # silently become unable to speak for itself
        raise ValueError("every device in CINDRAL_AGENT_TOKENS must have a distinct token")
    _INDEXES[id(tokens)] = (tokens, built)
    return built


def device_for(presented: str, tokens: Mapping[str, str]) -> str | None:
    """The device a presented token belongs to, or None when it belongs to none."""
    return index(tokens).get(hashlib.sha256(presented.encode()).digest())


class DeviceAuthMixin:
    """The two authorization gates every device-facing path goes through.

    A mixin rather than methods on the handler because the answer is a property of
    the credential and the configured map, not of HTTP: the same two questions are
    asked by five different handlers and none of them should be able to answer one
    of them its own way.
    """

    server: Any

    def _presented_agent_device(self) -> str | None:
        """Which device the presented credential speaks for, or None.

        With no per-device map this returns None whatever is presented, because
        nothing here can tell one device's credential from another's. The shared
        token is checked separately, by the coarse gate, and it identifies no
        device at all.
        """
        tokens = self.server.agent_tokens
        if not tokens:
            return None
        header = self.headers.get("Authorization", "")  # type: ignore[attr-defined]
        prefix = "Bearer "
        if not header.startswith(prefix):
            return None
        return device_for(header[len(prefix):].strip(), tokens)

    def _agent_authorized_as(self, device: str) -> bool:
        """Whether the presented credential speaks for *this* device.

        Called where the device name has been read, which is the only place it is
        known: the coarse gate runs before any body is parsed.
        """
        tokens = self.server.agent_tokens
        if not tokens:
            return self._token_authorized(self.server.agent_token)  # type: ignore[attr-defined]
        return self._presented_agent_device() == device

    def _agent_authorized(self) -> bool:
        """Whether the caller holds *a* device credential, whichever device.

        The gate in front of every device-facing path. It asks only that the caller
        be some device, and the path narrows that to the device the request names.
        """
        if self.server.agent_tokens:
            return self._presented_agent_device() is not None
        return self._token_authorized(self.server.agent_token)  # type: ignore[attr-defined]

    def _token_authorized(self, expected: str | None) -> bool:
        """Constant-time bearer check against one configured token.

        An unset token refuses rather than matching, so a missing secret can never
        silently open the path it guards.
        """
        if not expected:
            return False
        header = self.headers.get("Authorization", "")  # type: ignore[attr-defined]
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):].strip(), expected)

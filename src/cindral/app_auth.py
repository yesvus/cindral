"""Authenticate as a GitHub App installation.

An App is installed once for the whole account, so the token it mints is scoped
to exactly the repositories the installation covers. That is the reason to use
one: a user token can see every repository the user can see, including other
people's, and no code of ours can narrow that.

The token lives an hour, so it is minted on demand and cached until shortly
before it expires. A GitHub App JWT may not outlive ten minutes, and a refresh
must not be able to run a few seconds past that, so the minted window is well
inside the limit.

The private key and the token are never logged, never returned by a `dict`/`repr`
path, and never reach a response body.
"""
import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
import json
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

API_URL = "https://api.github.com"
# a GitHub App JWT may not be valid for more than ten minutes
JWT_TTL_SECONDS = 540
# minted this far before the stated expiry, so a token handed out cannot expire
# between the check and the use
TOKEN_REFRESH_MARGIN_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 15
API_VERSION = "2022-11-28"


class AppAuthError(RuntimeError):
    """Raised when the App cannot authenticate.

    The broker treats this as a startup refusal rather than a degraded mode: an
    App that cannot mint a token produces silent pushes, and silence is the one
    failure this project exists to remove.
    """


@dataclass(frozen=True)
class Installation:
    """One installation of the App, and the repositories it covers."""

    id: int
    account: str
    selection: str
    suspended_at: str | None

    @property
    def usable(self) -> bool:
        return self.suspended_at is None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def mint_jwt(client_id: str, private_key_pem: bytes, now: float | None = None) -> str:
    """Sign the RS256 JWT that authenticates the App itself.

    The claim set is deliberately minimal: `iat` is backdated so a small clock
    difference does not reject a token that is otherwise valid, and `exp` is
    inside the ten-minute ceiling.
    """
    issued = int(time.time() if now is None else now)
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64url(
        json.dumps(
            {"iat": issued - 60, "exp": issued + JWT_TTL_SECONDS, "iss": client_id},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode()
    try:
        key = serialization.load_pem_private_key(private_key_pem, password=None)
        # inside the guard because a key that loads but cannot sign this way
        # must raise the documented error, not a bare TypeError that health()
        # does not catch
        signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    except (ValueError, TypeError) as exc:
        # the message is the exception type only: a loader error can quote the
        # key material it choked on, and this string may be logged
        raise AppAuthError("the App private key could not be read") from None
    return f"{header}.{claims}.{_b64url(signature)}"


class AppAuth:
    """Mints and caches installation access tokens for one App.

    `transport` is injected so the token exchange can be tested without a
    network, and so a test can assert that no request ever carries the key.
    """

    def __init__(
        self,
        client_id: str,
        private_key_pem: bytes,
        api_url: str = API_URL,
        now: Callable[[], float] = time.time,
        transport: Callable[[str, str, dict | None], dict] | None = None,
    ) -> None:
        self.client_id = client_id
        self._private_key = private_key_pem
        self.api_url = api_url.rstrip("/")
        self._now = now
        self._transport = transport or self._request
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._installation: Installation | None = None
        # reentrant because a token is minted for the installation, and the
        # installation check reaches for a token to read its repositories
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        # the default dataclass-style repr is not used here, but a stray repr in
        # a log line must not be able to print the key or the token
        return f"AppAuth(client_id={self.client_id!r}, authenticated={self._token is not None})"

    def _request(self, path: str, token: str, body: dict | None = None) -> dict:
        request = Request(
            f"{self.api_url}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method="POST" if body is not None else "GET",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.load(response)
        except HTTPError as exc:
            # the status and the endpoint, never the body: a GitHub error body
            # can echo back the credential that was rejected
            raise AppAuthError(f"GitHub answered {exc.code} for {path}") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise AppAuthError(f"GitHub is unreachable for {path}: {type(exc).__name__}") from None

    def _app_token(self) -> str:
        return mint_jwt(self.client_id, self._private_key, self._now())

    def installations(self, per_page: int = 100, max_pages: int = 20) -> list[Installation]:
        """Every installation of the App, paged to a bound that raises.

        Paging matters more than it looks: an App installed on more accounts
        than one page holds would otherwise look unambiguous, and the broker
        would authenticate as whichever account the first page named.
        """
        entries: list = []
        # minted once: the JWT is valid for ten minutes and spans every page
        app_token = self._app_token()
        for page in range(1, max_pages + 1):
            found = self._transport(
                f"/app/installations?per_page={per_page}&page={page}", app_token
            )
            batch = found if isinstance(found, list) else found.get("installations", [])
            if not isinstance(batch, list):
                raise AppAuthError("GitHub returned an invalid installation list")
            entries.extend(batch)
            if len(batch) < per_page:
                return [
                    _installation(entry)
                    for entry in entries
                ]
        raise AppAuthError(
            f"the App is installed on at least {max_pages * per_page} accounts;"
            " name one, or narrow the App"
        )

    def installation(self) -> Installation:
        """The installation the broker serves, refusing anything unusable.

        Refusing here is the whole point: an App that is uninstalled, suspended,
        or scoped to nothing would otherwise leave every push undelivered with
        nothing in the logs to say why.
        """
        with self._lock:
            if self._installation is not None:
                return self._installation
            found = self.installations()
            if not found:
                raise AppAuthError("the App is not installed on any account")
            usable = [installation for installation in found if installation.usable]
            if not usable:
                raise AppAuthError("every installation of the App is suspended")
            if len(usable) > 1:
                names = ", ".join(f"{i.account}:{i.id}" for i in usable)
                raise AppAuthError(
                    f"the App has more than one usable installation, name one: {names}"
                )
            scoped = usable[0]
            # The cache is populated for the duration of the check, because the
            # check needs a token and minting one asks for the installation
            # again. It is cleared on every way out of it: a second request
            # thread reading the cache mid-check would otherwise hold the very
            # installation this is about to refuse, and a refusal that left the
            # token cached would hand a live credential to the next caller.
            self._installation = scoped
            try:
                # only an explicitly unrestricted installation skips the probe:
                # a value this module does not recognise must not quietly mean
                # "assume it covers something"
                if scoped.selection != "all" and not self._covers_anything():
                    # serving an installation scoped to no repositories would
                    # accept every push and dispatch none of them
                    raise AppAuthError(
                        f"installation {scoped.id} is scoped to selected repositories"
                        " and covers none of them"
                    )
            except BaseException:
                self._installation = None
                self._token = None
                self._token_expires_at = 0.0
                raise
            return scoped

    def token(self) -> str:
        """A current installation access token, minted or reused.

        The cached token is reused only while it has more than the refresh
        margin left, so a caller cannot be handed one that expires in flight.
        Freshness is re-checked under the lock, because the broker runs a thread
        per request and without it every thread of a cold burst minted its own
        token, multiplying the slowest call in the module by the burst size.
        """
        with self._lock:
            # read inside the lock: a caller that waited for it must be judged
            # against the clock as of the check, not as of before the wait
            now = self._now()
            if self._token is not None and now < self._token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
                return self._token
            installation = self.installation()
            # resolving the installation can mint one of its own, since a scoped
            # installation is checked by reading the repositories it covers, so
            # freshness is re-read rather than minting a second
            if self._token is not None and now < self._token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
                return self._token
            minted = self._transport(
                f"/app/installations/{installation.id}/access_tokens", self._app_token(), {}
            )
            token = minted.get("token")
            if not isinstance(token, str) or not token:
                raise AppAuthError("GitHub returned no installation token")
            self._token = token
            self._token_expires_at = _expiry(minted.get("expires_at"), now)
            return token

    def repositories(self, per_page: int = 100, max_pages: int = 50) -> list[str]:
        """Every repository the installation covers.

        Pages with a bounded cap, and a full page at the cap raises instead of
        returning a silent prefix: a truncated inventory is a set of
        repositories nobody chose to leave out, which is the failure this whole
        change exists to prevent.
        """
        found: list[str] = []
        for page in range(1, max_pages + 1):
            body = self._transport(f"/installation/repositories?per_page={per_page}&page={page}", self.token())
            if not isinstance(body, Mapping):
                raise AppAuthError("GitHub returned an invalid repository page")
            entries = body.get("repositories", [])
            found.extend(str(entry["full_name"]) for entry in entries)
            if len(entries) < per_page:
                return found
        raise AppAuthError(
            f"the App can see at least {max_pages * per_page} repositories;"
            " narrow the installation or raise the page cap explicitly"
        )

    def _covers_anything(self) -> bool:
        """Whether the installation covers at least one repository.

        One page of one, and no cap: the question is "is this empty", and the
        raise a bounded walk uses for a full page at its bound would answer a
        different one. A repository whose first page is full is not empty.
        """
        body = self._transport("/installation/repositories?per_page=1&page=1", self.token())
        if not isinstance(body, Mapping):
            raise AppAuthError("GitHub returned an invalid repository page")
        entries = body.get("repositories", [])
        if not isinstance(entries, list):
            raise AppAuthError("GitHub returned an invalid repository list")
        return bool(entries)

    def health(self, repositories: int | None = None) -> dict[str, object]:
        """A small summary for startup and for `/healthz`.

        Reports the installation and the token's remaining life, never the token.

        `repositories` is how much of GitHub the installation covers, which the
        caller has already resolved by the time it wants a summary. It is a
        parameter rather than something the caller adds to the result afterwards, so
        a probe cannot report an installation whose coverage nobody worked out, and
        the caller cannot forget to include it.
        """
        try:
            installation = self.installation()
        except AppAuthError as exc:
            return {"ok": False, "error": str(exc)}
        remaining = max(0, int(self._token_expires_at - self._now())) if self._token else 0
        summary: dict[str, object] = {
            "ok": True,
            "client_id": self.client_id,
            "installation": installation.id,
            "account": installation.account,
            "repository_selection": installation.selection,
            "token_expires_in_seconds": remaining,
        }
        if repositories is not None:
            summary["repositories"] = repositories
        return summary


def probe_block(app_auth: "AppAuth | None") -> dict[str, object] | None:
    """The App's summary for `/healthz`, resolved once, or None when unconfigured.

    Named rather than inlined in startup so the composition is reachable from a test.
    Everything startup wires is otherwise only exercised by starting a broker, which
    means the wiring a probe depends on can be wrong with the whole suite green: a
    caller that resolved the coverage and then failed to hand it over produced a
    healthy-looking installation whose reach nobody could see.
    """
    if app_auth is None:
        return None
    covered = app_auth.repositories()
    return app_auth.health(repositories=len(covered))


def startup_report(block: dict[str, object]) -> str:
    """The line startup prints about the App, healthy or not.

    Named so the sentence is exercised rather than restated. A test that rebuilds
    the string grades its own copy, and the copy keeps passing while the line an
    operator reads at boot is deleted, shortened or made to raise.

    The block is not fixed-shape: `health()` answers `{"ok": False, "error": ...}`
    when the installation cannot be read, so the degraded case is named as itself
    rather than indexing a key it does not carry.
    """
    if not block.get("ok"):
        return f"the GitHub App is not usable: {block.get('error', 'no reason given')}"
    return (
        f"authenticating as the {block['account']} App installation"
        f" {block['installation']}, covering {block['repositories']} repositories"
    )


def _installation(entry: object) -> Installation:
    """One installation record, or a refusal rather than a raised KeyError.

    ``account: null`` is a shape real installations produce, and ``health()``
    catches only AppAuthError, so a missing field has to arrive as one.
    """
    if not isinstance(entry, Mapping):
        raise AppAuthError("GitHub returned an invalid installation entry")
    identifier = entry.get("id")
    if isinstance(identifier, bool) or not isinstance(identifier, int):
        raise AppAuthError("GitHub returned an installation without an id")
    account = entry.get("account")
    return Installation(
        id=identifier,
        account=str(account.get("login", "")) if isinstance(account, Mapping) else "",
        selection=str(entry.get("repository_selection", "")),
        suspended_at=entry.get("suspended_at"),
    )


def _expiry(value: object, now: float) -> float:
    """Read GitHub's ISO expiry, falling back to the known one-hour lifetime.

    A missing or unparseable expiry must not become "never expires", which would
    leave a dead token cached until a call failed.
    """
    claimed = now + 3600
    if isinstance(value, str) and value:
        try:
            stated = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        else:
            # a stated expiry past the lifetime this fallback reasons about is
            # not evidence of a longer life, and caching to it would keep
            # serving a token GitHub has already invalidated
            claimed = min(stated, now + 3600)
    return claimed

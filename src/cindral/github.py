"""GitHub workflow dispatch client."""
import json
from collections.abc import Callable
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .contract import CONTRACT_PATH
from .merge_ref import pull_request_merge_sha


class GitHubDispatchError(RuntimeError):
    """Raised when GitHub rejects a workflow dispatch."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


from .errors import GitHubAPIError  # re-exported: many modules catch it from here

STATUS_STATES = ("error", "failure", "pending", "success")
# GitHub renders a commit status description up to this many characters
MAX_STATUS_DESCRIPTION = 140
# adoption reads one repository inventory at 100 per page; past this many pages
# the run stops rather than silently adopting a prefix of the account
MAX_REPOSITORY_PAGES = 5


@dataclass(frozen=True)
class DiscoveredRepository:
    """One repository the credential can see, with its adoption metadata."""

    repository: str
    private: bool
    archived: bool
    default_branch: str
    has_contract: bool
    has_workflows: bool


@dataclass(frozen=True)
class RepositoryRunner:
    name: str
    status: str
    labels: tuple[str, ...]
    busy: bool = False


def is_repository_slug(repository: str) -> bool:
    parts = repository.split("/")
    return (
        len(parts) == 2
        and all(part not in {"", ".", ".."} for part in parts)
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is not None
    )


class GitHubClient:
    def __init__(self, token: str | Callable[[], str], api_url: str = "https://api.github.com") -> None:
        self._token = token
        self.api_url = api_url.rstrip("/")

    def _credential(self) -> str:
        """The bearer value for one request.

        An App installation token expires within the hour, so the broker is given
        a provider to call per request rather than a token to hold. Resolving it
        here is what lets a long-running broker keep working past the first
        expiry, and it is the only place the stored credential is read, so a
        provider cannot be interpolated into a header as its own repr.
        """
        return self._token() if callable(self._token) else self._token

    def _read_headers(self, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._credential()}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def dispatch(self, repository: str, workflow: str, ref: str, inputs: dict[str, str]) -> None:
        url = f"{self.api_url}/repos/{repository}/actions/workflows/{quote(workflow, safe='')}/dispatches"
        request = Request(
            url,
            data=json.dumps({"ref": ref, "inputs": inputs}).encode(),
            headers=self._read_headers(json_body=True),
            method="POST",
        )
        try:
            with urlopen(request, timeout=30):
                pass
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise GitHubDispatchError(f"GitHub dispatch failed with HTTP {exc.code}: {detail}", exc.code) from exc
        except URLError as exc:
            raise GitHubDispatchError(f"GitHub dispatch failed: {exc.reason}") from exc

    def workflow_exists(self, repository: str, workflow: str) -> bool:
        if not is_repository_slug(repository):
            raise ValueError("repository must use the owner/name format")
        url = f"{self.api_url}/repos/{quote(repository, safe='/')}/actions/workflows/{quote(workflow, safe='')}"
        request = Request(
            url,
            headers=self._read_headers(),
        )
        try:
            with urlopen(request, timeout=30):
                return True
        except HTTPError as exc:
            if exc.code == 404:
                return False
            detail = exc.read().decode(errors="replace")
            raise GitHubAPIError(f"GitHub workflow lookup failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise GitHubAPIError(f"GitHub workflow lookup failed: {exc.reason}") from exc

    def pull_request_merge_sha(self, repository: str, number: int) -> tuple[str, tuple[str, ...]] | None:
        # the reasoning is in merge_ref: a trust question, not a GitHub call
        if not is_repository_slug(repository):
            raise ValueError("repository must use the owner/name format")
        url = f"{self.api_url}/repos/{repository}"
        return pull_request_merge_sha(url, self._read_headers(), repository, number)

    def list_repositories(
        self, max_pages: int = MAX_REPOSITORY_PAGES, *, probe: bool = True
    ) -> tuple[DiscoveredRepository, ...]:
        """List the repositories the credential owns.

        Being a collaborator or an organization member is not consent to hook a
        repository, so only owned ones are listed and only owned ones adopt.
        The page count is hard-capped and a full page at the cap raises rather
        than adopting a silent prefix, because an incomplete inventory must not
        decide adoption. With ``probe`` false the per-repository tree lookups
        are skipped: two requests each, feeding only the definition note that
        no applying pass prints, and unanswerable ones stop the run.
        """
        if max_pages < 1:
            raise ValueError("max_pages must be at least one")
        discovered: list[DiscoveredRepository] = []
        for page in range(1, max_pages + 1):
            entries = self._list_repository_page(page)
            for entry in entries:
                if not isinstance(entry, dict):
                    raise GitHubAPIError("GitHub returned an invalid repository list")
                discovered.append(self._repository(entry, probe))
            if len(entries) < 100:
                return tuple(discovered)
        if self._list_repository_page(max_pages + 1):
            raise GitHubAPIError(
                f"GitHub returned more than {max_pages * 100} repositories; "
                "narrow the credential's scope or raise the page cap explicitly"
            )
        return tuple(discovered)

    def _repository(self, entry: dict, probe: bool) -> DiscoveredRepository:
        """One listed repository, or a refusal to trust the listing at all.

        An empty repository reports no default branch, which is one repository
        that cannot be adopted rather than an inventory that cannot be trusted,
        so it becomes an empty branch name and the caller skips it with a
        reason. Refusing the whole account here is what made one empty
        repository render the command unusable.
        """
        name = entry.get("full_name")
        if not isinstance(name, str) or not is_repository_slug(name):
            raise GitHubAPIError("GitHub returned a repository without an owner/name slug")
        if not isinstance(entry.get("private"), bool) or not isinstance(entry.get("archived"), bool):
            raise GitHubAPIError("GitHub returned an invalid repository inventory")
        branch = entry.get("default_branch")
        if branch is not None and not isinstance(branch, str):
            raise GitHubAPIError("GitHub returned an invalid repository inventory")
        return DiscoveredRepository(
            repository=name,
            private=entry["private"],
            archived=entry["archived"],
            default_branch=branch or "",
            has_contract=self._path_exists(name, CONTRACT_PATH) if probe else False,
            has_workflows=self._has_workflows_directory(name) if probe else False,
        )

    def _list_repository_page(self, page: int) -> list[dict[str, object]]:
        query = f"affiliation=owner&sort=full_name&per_page=100&page={page}"
        request = self._get(f"{self.api_url}/user/repos?{query}")
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read())
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise GitHubAPIError(f"GitHub repository listing failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise GitHubAPIError(f"GitHub repository listing failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise GitHubAPIError("GitHub returned an invalid repository list") from exc
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise GitHubAPIError("GitHub returned an invalid repository inventory")
        return payload

    def _get(self, url: str) -> Request:
        return Request(
            url,
            headers=self._read_headers(),
        )

    def _has_workflows_directory(self, repository: str) -> bool:
        listing = self._contents(repository, ".github/workflows")
        return isinstance(listing, list) and bool(listing)

    def _contents(self, repository: str, path: str) -> object:
        url = f"{self.api_url}/repos/{quote(repository, safe='/')}/contents/{path}"
        request = self._get(url)
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            if exc.code == 404:
                return None
            detail = exc.read().decode(errors="replace")
            raise GitHubAPIError(f"GitHub contents lookup failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise GitHubAPIError(f"GitHub contents lookup failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise GitHubAPIError("GitHub returned an invalid contents response") from exc

    def _path_exists(self, repository: str, path: str) -> bool:
        return self._contents(repository, path) is not None

    def push_webhook_urls(self, repository: str) -> tuple[str, ...]:
        """Where this repository's push webhooks point, read without changing them.

        A repository whose webhook drifted is the one that silently stops
        dispatching, and `reconcile-webhooks` can only report that it would fix
        something. A status report has to be able to say it is already correct.
        """
        if not is_repository_slug(repository):
            raise ValueError("repository must use the owner/name format")
        found: list[str] = []
        for hook in self._list_hooks(repository):
            config = hook.get("config") if isinstance(hook, dict) else None
            if not isinstance(config, dict):
                continue
            # `active` is the field GitHub flips when an operator switches a
            # hook off, and the hook stays listed with its url and events
            # intact. Reporting it as wired describes a repository that will
            # never dispatch, which is the exact drift this exists to catch.
            if hook.get("active") is False:
                continue
            url = config.get("url")
            # GitHub returns `events` as an array of names, never as the string
            # "push", so a string compare reads a correctly wired hook as one
            # that does not listen, and a status report then tells the operator
            # to re-register a webhook that is already registered.
            events = hook.get("events")
            if isinstance(events, list) and "push" in events and isinstance(url, str) and url:
                found.append(url)
        return tuple(found)

    def ensure_push_webhook(self, repository: str, url: str, secret: str) -> str:
        if not is_repository_slug(repository):
            raise ValueError("repository must use the owner/name format")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("webhook URL must be an HTTPS URL without embedded credentials")
        if not secret:
            raise ValueError("webhook secret must not be empty")

        hooks = self._list_hooks(repository)
        existing = next(
            (
                hook
                for hook in hooks
                if isinstance(hook, dict)
                and isinstance(hook.get("config"), dict)
                and hook["config"].get("url") == url
            ),
            None,
        )
        payload = {
            "active": True,
            # pull_request deliveries let direct repositories queue trusted,
            # same-repository PRs on the device pool
            "events": ["push", "pull_request"],
            "config": {
                "url": url,
                "content_type": "json",
                "secret": secret,
                "insecure_ssl": "0",
            },
        }
        if existing is None:
            endpoint = f"{self.api_url}/repos/{quote(repository, safe='/')}/hooks"
            method = "POST"
            result = "created"
        else:
            hook_id = existing.get("id")
            if not isinstance(hook_id, int):
                raise GitHubAPIError("GitHub returned a webhook without a numeric id")
            endpoint = f"{self.api_url}/repos/{quote(repository, safe='/')}/hooks/{hook_id}"
            method = "PATCH"
            result = "updated"
        request = Request(
            endpoint,
            data=json.dumps(payload).encode(),
            headers=self._read_headers(json_body=True),
            method=method,
        )
        try:
            with urlopen(request, timeout=30):
                pass
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise GitHubAPIError(f"GitHub webhook setup failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise GitHubAPIError(f"GitHub webhook setup failed: {exc.reason}") from exc
        return result

    def _list_hooks(self, repository: str) -> list[dict[str, object]]:
        hooks: list[dict[str, object]] = []
        page = 1
        while True:
            url = f"{self.api_url}/repos/{quote(repository, safe='/')}/hooks?per_page=100&page={page}"
            request = Request(
                url,
                headers=self._read_headers(),
            )
            try:
                with urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read())
            except HTTPError as exc:
                detail = exc.read().decode(errors="replace")
                raise GitHubAPIError(f"GitHub webhook lookup failed with HTTP {exc.code}: {detail}") from exc
            except URLError as exc:
                raise GitHubAPIError(f"GitHub webhook lookup failed: {exc.reason}") from exc
            except json.JSONDecodeError as exc:
                raise GitHubAPIError("GitHub returned an invalid webhook list") from exc
            if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
                raise GitHubAPIError("GitHub returned an invalid webhook list")
            hooks.extend(payload)
            if len(payload) < 100:
                return hooks
            page += 1

    def list_runners(self, repository: str) -> tuple[RepositoryRunner, ...]:
        if not is_repository_slug(repository):
            raise ValueError("repository must use the owner/name format")
        runners: list[RepositoryRunner] = []
        page = 1
        while True:
            url = f"{self.api_url}/repos/{quote(repository, safe='/')}/actions/runners?per_page=100&page={page}"
            request = Request(
                url,
                headers=self._read_headers(),
            )
            try:
                with urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read())
            except HTTPError as exc:
                detail = exc.read().decode(errors="replace")
                raise GitHubAPIError(f"GitHub runner lookup failed with HTTP {exc.code}: {detail}") from exc
            except URLError as exc:
                raise GitHubAPIError(f"GitHub runner lookup failed: {exc.reason}") from exc
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise GitHubAPIError("GitHub returned an invalid runner list") from exc

            if not isinstance(payload, dict):
                raise GitHubAPIError("GitHub returned an invalid runner list")
            page_runners = payload.get("runners")
            if not isinstance(page_runners, list):
                raise GitHubAPIError("GitHub returned an invalid runner list")
            try:
                runners.extend(
                    RepositoryRunner(
                        name=str(item["name"]),
                        status=str(item.get("status", "offline")),
                        busy=bool(item.get("busy", False)),
                        labels=tuple(str(label["name"]) for label in item.get("labels", [])),
                    )
                    for item in page_runners
                )
            except (KeyError, TypeError) as exc:
                raise GitHubAPIError("GitHub returned an invalid runner list") from exc
            if len(page_runners) < 100:
                return tuple(runners)
            page += 1

    def post_status(
        self,
        repository: str,
        sha: str,
        state: str,
        description: str = "",
        context: str = "cindral/ci",
        target_url: str = "",
        timeout: int = 30,
    ) -> None:
        if not is_repository_slug(repository):
            raise ValueError("repository must use the owner/name format")
        if state not in STATUS_STATES:
            raise ValueError(f"invalid status state: {state}")
        if not sha:
            raise ValueError("status requires a commit sha")
        url = f"{self.api_url}/repos/{quote(repository, safe='/')}/statuses/{quote(sha, safe='')}"
        payload: dict[str, str] = {
            "state": state,
            "description": description[:MAX_STATUS_DESCRIPTION],
            "context": context,
        }
        if target_url:
            payload["target_url"] = target_url
        request = Request(
            url,
            data=json.dumps(payload).encode(),
            headers=self._read_headers(json_body=True),
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout):
                pass
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise GitHubAPIError(f"GitHub status update failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise GitHubAPIError(f"GitHub status update failed: {exc.reason}") from exc

"""GitHub workflow dispatch client."""
import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


class GitHubDispatchError(RuntimeError):
    """Raised when GitHub rejects a workflow dispatch."""


class GitHubAPIError(RuntimeError):
    """Raised when a GitHub API request fails."""


@dataclass(frozen=True)
class RepositoryRunner:
    name: str
    status: str
    labels: tuple[str, ...]


def is_repository_slug(repository: str) -> bool:
    parts = repository.split("/")
    return (
        len(parts) == 2
        and all(part not in {"", ".", ".."} for part in parts)
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is not None
    )


class GitHubClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com") -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")

    def dispatch(self, repository: str, workflow: str, ref: str, inputs: dict[str, str]) -> None:
        url = f"{self.api_url}/repos/{repository}/actions/workflows/{quote(workflow, safe='')}/dispatches"
        request = Request(
            url,
            data=json.dumps({"ref": ref, "inputs": inputs}).encode(),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=30):
                pass
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise GitHubDispatchError(f"GitHub dispatch failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise GitHubDispatchError(f"GitHub dispatch failed: {exc.reason}") from exc

    def workflow_exists(self, repository: str, workflow: str) -> bool:
        if not is_repository_slug(repository):
            raise ValueError("repository must use the owner/name format")
        url = f"{self.api_url}/repos/{quote(repository, safe='/')}/actions/workflows/{quote(workflow, safe='')}"
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
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
            "events": ["push"],
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
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
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
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
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
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
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
                        labels=tuple(str(label["name"]) for label in item.get("labels", [])),
                    )
                    for item in page_runners
                )
            except (KeyError, TypeError) as exc:
                raise GitHubAPIError("GitHub returned an invalid runner list") from exc
            if len(page_runners) < 100:
                return tuple(runners)
            page += 1

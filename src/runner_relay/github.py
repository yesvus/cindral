"""GitHub workflow dispatch client."""
import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote
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

"""GitHub workflow dispatch client."""
import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class GitHubDispatchError(RuntimeError):
    """Raised when GitHub rejects a workflow dispatch."""


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

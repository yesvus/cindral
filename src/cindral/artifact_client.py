"""HTTP client a device uses to upload one run's artifact archive.

The archive rides as its own request rather than inside the report: a report is
a JSON body the broker caps at a megabyte, and a run's output is neither JSON
nor a megabyte. It is sent before the report, so the run's verdict is the last
thing the broker hears about it and a reader who opens the run as soon as it
finishes already finds the archive.
"""
import http.client
import json
import os
from typing import Any
from urllib.parse import quote
import urllib.error
import urllib.request

from .artifact import ARTIFACT_PATH, MAX_ARTIFACT_BYTES


class ArtifactClientError(RuntimeError):
    pass


class CindralArtifactClient:
    """Uploads through the agent's own connection, so it holds no credential."""

    def __init__(self, client: Any, timeout: int = 30) -> None:
        self._client = client
        self.timeout = timeout

    def upload(self, job_id: str, device: str, archive: str) -> dict:
        length = os.path.getsize(archive)
        # the broker refuses anything past its own cap, so the device refuses
        # the same one rather than spending the upload to learn that
        if length > MAX_ARTIFACT_BYTES:
            raise ArtifactClientError("artifact archive exceeds the upload size limit")
        with open(archive, "rb") as body:
            request = urllib.request.Request(
                f"{self._client.base_url}"
                + ARTIFACT_PATH.format(job_id=quote(job_id, safe="")),
                data=body,
                headers={
                    "Authorization": f"Bearer {self._client.token}",
                    "Content-Type": "application/x-tar",
                    "Content-Length": str(length),
                    "X-Cindral-Device": device,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    status = response.status
                    payload = response.read()
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")
                raise ArtifactClientError(
                    f"artifact upload failed with HTTP {exc.code}: {detail}"
                ) from exc
            except (OSError, http.client.HTTPException) as exc:
                raise ArtifactClientError(f"artifact upload failed: {exc}") from exc
        if status not in {200, 201}:
            raise ArtifactClientError(f"artifact upload failed with HTTP {status}")
        if not payload:
            return {}
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArtifactClientError("artifact upload returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ArtifactClientError("artifact upload returned a non-object response")
        return value

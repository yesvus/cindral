"""HTTP client for broker-hosted cache operations."""
import hashlib
import http.client
import json
from pathlib import Path
import ssl
from urllib.parse import quote, urlsplit

from .cache import MAX_CACHE_BLOB_BYTES


class CacheClientError(RuntimeError):
    pass


class CindralCacheClient:
    def __init__(self, base_url: str, token: str, timeout: int = 30) -> None:
        self.url = urlsplit(base_url)
        if self.url.scheme not in {"http", "https"} or not self.url.hostname:
            raise ValueError("cache URL must be http or https")
        self.base_path = self.url.path.rstrip("/")
        self.token = token
        self.timeout = timeout

    def lookup(
        self,
        repository: str,
        branch: str,
        architecture: str,
        key: str,
        restore_keys: tuple[str, ...] = (),
    ) -> dict | None:
        payload = json.dumps(
            {
                "repository": repository,
                "branch": branch,
                "architecture": architecture,
                "key": key,
                "restore_keys": list(restore_keys),
            }
        ).encode()
        status, _, body = self._request(
            "POST", "/v1/cache/lookup", {"Content-Type": "application/json"}, payload
        )
        if status != 200:
            raise CacheClientError(f"cache lookup failed with HTTP {status}: {body.decode(errors='replace')}")
        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise CacheClientError("cache lookup returned invalid JSON") from exc
        if not isinstance(result, dict) or not isinstance(result.get("hit"), bool):
            raise CacheClientError("cache lookup returned an invalid response")
        entry = result.get("entry")
        if result["hit"] and not isinstance(entry, dict):
            raise CacheClientError("cache lookup returned an invalid entry")
        return entry if result["hit"] else None

    def download(
        self,
        digest: str,
        destination: Path,
        repository: str,
        branch: str,
        architecture: str,
        key: str,
    ) -> None:
        connection, response = self._open(
            "GET",
            f"/v1/cache/blobs/{quote(digest, safe='')}",
            {
                "X-Cindral-Repository": repository,
                "X-Cindral-Branch": branch,
                "X-Cindral-Architecture": architecture,
                "X-Cindral-Key": key,
            },
        )
        try:
            if response.status != 200:
                body = response.read(1024 * 1024).decode(errors="replace")
                raise CacheClientError(f"cache download failed with HTTP {response.status}: {body}")
            length = int(response.getheader("Content-Length", "-1"))
            if length < 0 or length > MAX_CACHE_BLOB_BYTES:
                raise CacheClientError("cache download has an invalid size")
            digest_state = hashlib.sha256()
            written = 0
            with destination.open("wb") as output:
                while block := response.read(1024 * 1024):
                    output.write(block)
                    digest_state.update(block)
                    written += len(block)
            if written != length or digest_state.hexdigest() != digest:
                destination.unlink(missing_ok=True)
                raise CacheClientError("cache blob failed its content digest check")
        finally:
            response.close()
            connection.close()

    def upload(
        self,
        repository: str,
        branch: str,
        architecture: str,
        key: str,
        source: Path,
    ) -> dict:
        length = source.stat().st_size
        if length > MAX_CACHE_BLOB_BYTES:
            raise CacheClientError("cache archive exceeds the upload size limit")
        path = self._path("/v1/cache/entries")
        connection = self._connection()
        try:
            connection.putrequest("PUT", path)
            for name, value in (
                ("Authorization", f"Bearer {self.token}"),
                ("Content-Type", "application/octet-stream"),
                ("Content-Length", str(length)),
                ("X-Cindral-Repository", repository),
                ("X-Cindral-Branch", branch),
                ("X-Cindral-Architecture", architecture),
                ("X-Cindral-Key", key),
            ):
                connection.putheader(name, value)
            connection.endheaders()
            with source.open("rb") as archive:
                while block := archive.read(1024 * 1024):
                    connection.send(block)
            response = connection.getresponse()
            body = response.read(1024 * 1024)
            if response.status not in {200, 201}:
                raise CacheClientError(
                    f"cache upload failed with HTTP {response.status}: {body.decode(errors='replace')}"
                )
            try:
                result = json.loads(body)
            except json.JSONDecodeError as exc:
                raise CacheClientError("cache upload returned invalid JSON") from exc
            if not isinstance(result, dict) or not isinstance(result.get("entry"), dict):
                raise CacheClientError("cache upload returned an invalid response")
            return result
        finally:
            connection.close()

    def _request(self, method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict, bytes]:
        connection = self._connection()
        try:
            connection.request(
                method,
                self._path(path),
                body=body,
                headers={"Authorization": f"Bearer {self.token}", **headers},
            )
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read(1024 * 1024)
        finally:
            connection.close()

    def _open(self, method: str, path: str, headers: dict[str, str] | None = None):
        connection = self._connection()
        connection.request(
            method,
            self._path(path),
            headers={"Authorization": f"Bearer {self.token}", **(headers or {})},
        )
        response = connection.getresponse()
        return connection, response

    def _connection(self):
        connection_type = http.client.HTTPSConnection if self.url.scheme == "https" else http.client.HTTPConnection
        kwargs = {"timeout": self.timeout}
        if self.url.scheme == "https":
            kwargs["context"] = ssl.create_default_context()
        return connection_type(self.url.hostname, self.url.port, **kwargs)

    def _path(self, path: str) -> str:
        return f"{self.base_path}{path}"

"""The cache endpoints, which are a read and a write and nothing else.

Separate from the job endpoints because a cache request carries no queue state:
it is authenticated, it names a digest or a key, and it never has to decide what
to do with a lease.
"""
from urllib.parse import urlsplit
import json

from .cache import MAX_CACHE_BLOB_BYTES, CacheError, CacheStore


class CacheApiMixin:
    def _cache(self) -> None:
        store: CacheStore | None = self.server.cache
        if store is None:
            self._send(503, {"error": "cache is not configured"})  # type: ignore[attr-defined]
            return
        if not self._agent_authorized():  # type: ignore[attr-defined]
            self._send(401, {"error": "cache requests require a bearer token"})  # type: ignore[attr-defined]
            return
        path = urlsplit(self.path).path  # type: ignore[attr-defined]
        try:
            if path == "/v1/cache/lookup" and self.command == "POST":  # type: ignore[attr-defined]
                self._cache_lookup(store)
                return
            if path == "/v1/cache/entries" and self.command == "PUT":  # type: ignore[attr-defined]
                self._cache_put(store)
                return
            if path.startswith("/v1/cache/blobs/") and self.command == "GET":  # type: ignore[attr-defined]
                self._cache_blob(store, path.removeprefix("/v1/cache/blobs/"))
                return
            self._send(404, {"error": "not found"})  # type: ignore[attr-defined]
        except (CacheError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})  # type: ignore[attr-defined]

    def _cache_lookup(self, store: CacheStore) -> None:
        payload = self._read_json()  # type: ignore[attr-defined]
        fields = ("repository", "branch", "architecture", "key")
        if any(not isinstance(payload.get(field), str) for field in fields):
            raise ValueError("repository, branch, architecture, and key must be strings")
        restore_keys = payload.get("restore_keys", [])
        if not isinstance(restore_keys, list) or not all(isinstance(key, str) for key in restore_keys):
            raise ValueError("restore_keys must be an array of strings")
        entry = store.lookup(
            payload["repository"],
            payload["branch"],
            payload["architecture"],
            payload["key"],
            tuple(restore_keys),
        )
        self._send(200, {"hit": entry is not None, "entry": entry.as_dict() if entry else None})  # type: ignore[attr-defined]

    def _cache_put(self, store: CacheStore) -> None:
        try:
            length = int(self.headers.get("Content-Length", "-1"))  # type: ignore[attr-defined]
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > min(store.max_blob_bytes, MAX_CACHE_BLOB_BYTES):
            self._send(413, {"error": "cache blob exceeds the configured size limit"})  # type: ignore[attr-defined]
            return
        entry, created = store.put(
            self.headers.get("X-Cindral-Repository", ""),  # type: ignore[attr-defined]
            self.headers.get("X-Cindral-Branch", ""),  # type: ignore[attr-defined]
            self.headers.get("X-Cindral-Architecture", ""),  # type: ignore[attr-defined]
            self.headers.get("X-Cindral-Key", ""),  # type: ignore[attr-defined]
            self.rfile,  # type: ignore[attr-defined]
            length,
        )
        self._send(201 if created else 200, {"created": created, "entry": entry.as_dict()})  # type: ignore[attr-defined]

    def _cache_blob(self, store: CacheStore, digest: str) -> None:
        try:
            blob, size = store.open_blob(
                digest,
                self.headers.get("X-Cindral-Repository", ""),  # type: ignore[attr-defined]
                self.headers.get("X-Cindral-Branch", ""),  # type: ignore[attr-defined]
                self.headers.get("X-Cindral-Architecture", ""),  # type: ignore[attr-defined]
                self.headers.get("X-Cindral-Key", ""),  # type: ignore[attr-defined]
            )
        except FileNotFoundError:
            self._send(404, {"error": "unknown cache blob"})  # type: ignore[attr-defined]
            return
        self.send_response(200)  # type: ignore[attr-defined]
        self.send_header("Content-Type", "application/octet-stream")  # type: ignore[attr-defined]
        self.send_header("Content-Length", str(size))  # type: ignore[attr-defined]
        self.send_header("ETag", f'"sha256:{digest}"')  # type: ignore[attr-defined]
        self.end_headers()  # type: ignore[attr-defined]
        with blob:
            while block := blob.read(1024 * 1024):
                self.wfile.write(block)  # type: ignore[attr-defined]

"""Persistent content-addressed cache scoped to repositories and branches."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from typing import BinaryIO

CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
CACHE_REPOSITORY_QUOTA = 256 * 1024 * 1024
CACHE_STORAGE_QUOTA = 768 * 1024 * 1024
MAX_CACHE_BLOB_BYTES = 256 * 1024 * 1024
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH = re.compile(r"^[A-Za-z0-9_./-]{1,255}$")
_ARCHITECTURE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


class CacheError(ValueError):
    pass


@dataclass(frozen=True)
class CacheEntry:
    repository: str
    branch: str
    architecture: str
    key: str
    digest: str
    size: int
    created_at: float

    def as_dict(self) -> dict[str, str | int | float]:
        return {
            "repository": self.repository,
            "branch": self.branch,
            "architecture": self.architecture,
            "key": self.key,
            "digest": self.digest,
            "size": self.size,
            "created_at": self.created_at,
        }


class CacheStore:
    def __init__(
        self,
        directory: str | Path,
        ttl_seconds: int = CACHE_TTL_SECONDS,
        repository_quota: int = CACHE_REPOSITORY_QUOTA,
        storage_quota: int = CACHE_STORAGE_QUOTA,
        max_blob_bytes: int = MAX_CACHE_BLOB_BYTES,
    ) -> None:
        if min(ttl_seconds, repository_quota, storage_quota, max_blob_bytes) < 1:
            raise ValueError("cache limits must be positive")
        self.directory = Path(directory)
        self.blobs = self.directory / "blobs" / "sha256"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / "cache.db"
        self.ttl_seconds = ttl_seconds
        self.repository_quota = repository_quota
        self.storage_quota = storage_quota
        self.max_blob_bytes = max_blob_bytes
        self._lock = threading.RLock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _init(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cache_entries (
                    repository TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    architecture TEXT NOT NULL,
                    key TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    accessed_at REAL NOT NULL,
                    PRIMARY KEY (repository, branch, architecture, key)
                );
                CREATE INDEX IF NOT EXISTS cache_entries_lru
                    ON cache_entries(repository, accessed_at, created_at);
                CREATE TABLE IF NOT EXISTS cache_stats (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO cache_stats(name, value) VALUES
                    ('hits', 0), ('misses', 0), ('writes', 0), ('bytes_written', 0);
                """
            )

    @staticmethod
    def validate_scope(repository: str, branch: str, architecture: str, key: str) -> None:
        if (
            not _REPOSITORY.fullmatch(repository)
            or any(part in {".", ".."} for part in repository.split("/"))
        ):
            raise CacheError("repository must use owner/name format")
        if not _BRANCH.fullmatch(branch) or ".." in branch:
            raise CacheError("invalid branch")
        if not _ARCHITECTURE.fullmatch(architecture):
            raise CacheError("invalid architecture")
        if not _KEY.fullmatch(key):
            raise CacheError("invalid cache key")

    def lookup(
        self,
        repository: str,
        branch: str,
        architecture: str,
        key: str,
        restore_keys: tuple[str, ...] = (),
        now: float | None = None,
    ) -> CacheEntry | None:
        self.validate_scope(repository, branch, architecture, key)
        if len(restore_keys) > 20 or any(not _KEY.fullmatch(prefix) for prefix in restore_keys):
            raise CacheError("restore_keys must contain at most 20 valid prefixes")
        current = time.time() if now is None else now
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection, current)
            self._collect_blobs(connection)
            row = connection.execute(
                "SELECT * FROM cache_entries WHERE repository=? AND branch=? AND architecture=? AND key=?",
                (repository, branch, architecture, key),
            ).fetchone()
            if row is None:
                for prefix in restore_keys:
                    row = connection.execute(
                        "SELECT * FROM cache_entries WHERE repository=? AND branch=? AND architecture=?"
                        " AND substr(key, 1, ?) = ? ORDER BY accessed_at DESC, key LIMIT 1",
                        (repository, branch, architecture, len(prefix), prefix),
                    ).fetchone()
                    if row is not None:
                        break
            counter = "hits" if row is not None else "misses"
            connection.execute("UPDATE cache_stats SET value=value+1 WHERE name=?", (counter,))
            if row is not None:
                connection.execute(
                    "UPDATE cache_entries SET accessed_at=? WHERE repository=? AND branch=?"
                    " AND architecture=? AND key=?",
                    (current, repository, branch, architecture, row["key"]),
                )
            connection.execute("COMMIT")
        if row is None:
            return None
        entry = self._entry(row)
        if not self.blob_path(entry.digest).is_file():
            with self._lock, self._connection() as connection:
                connection.execute(
                    "DELETE FROM cache_entries WHERE repository=? AND branch=? AND architecture=? AND key=?",
                    (repository, branch, architecture, entry.key),
                )
                connection.execute("UPDATE cache_stats SET value=value-1 WHERE name='hits'")
                connection.execute("UPDATE cache_stats SET value=value+1 WHERE name='misses'")
            return None
        return entry

    def put(
        self,
        repository: str,
        branch: str,
        architecture: str,
        key: str,
        source: BinaryIO,
        content_length: int,
        now: float | None = None,
    ) -> tuple[CacheEntry, bool]:
        self.validate_scope(repository, branch, architecture, key)
        if content_length < 0 or content_length > self.max_blob_bytes:
            raise CacheError("cache blob exceeds the configured size limit")
        if content_length > self.repository_quota:
            raise CacheError("cache blob exceeds the repository quota")
        current = time.time() if now is None else now
        fd, temporary = tempfile.mkstemp(prefix="upload-", dir=self.directory)
        digest = hashlib.sha256()
        written = 0
        try:
            with os.fdopen(fd, "wb") as target:
                while written < content_length:
                    block = source.read(min(1024 * 1024, content_length - written))
                    if not block:
                        raise CacheError("cache upload ended before Content-Length")
                    target.write(block)
                    digest.update(block)
                    written += len(block)
                target.flush()
                os.fsync(target.fileno())
            hexdigest = digest.hexdigest()
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM cache_entries WHERE repository=? AND branch=? AND architecture=? AND key=?",
                    (repository, branch, architecture, key),
                ).fetchone()
                if existing is not None:
                    connection.execute("COMMIT")
                    return self._entry(existing), False
                blob = self.blob_path(hexdigest)
                blob.parent.mkdir(parents=True, exist_ok=True)
                if blob.exists():
                    os.unlink(temporary)
                else:
                    os.replace(temporary, blob)
                connection.execute(
                    "INSERT INTO cache_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (repository, branch, architecture, key, hexdigest, written, current, current),
                )
                connection.execute("UPDATE cache_stats SET value=value+1 WHERE name='writes'")
                connection.execute(
                    "UPDATE cache_stats SET value=value+? WHERE name='bytes_written'", (written,)
                )
                self._expire(connection, current)
                self._enforce_quota(connection, repository)
                connection.execute("COMMIT")
            return CacheEntry(repository, branch, architecture, key, hexdigest, written, current), True
        except Exception:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise

    def open_blob(self, digest: str) -> tuple[BinaryIO, int]:
        if not _DIGEST.fullmatch(digest):
            raise CacheError("invalid content digest")
        path = self.blob_path(digest)
        if not path.is_file():
            raise FileNotFoundError(digest)
        blob = path.open("rb")
        return blob, os.fstat(blob.fileno()).st_size

    def blob_path(self, digest: str) -> Path:
        if not _DIGEST.fullmatch(digest):
            raise CacheError("invalid content digest")
        return self.blobs / digest[:2] / digest[2:]

    def metrics(self) -> str:
        with self._connection() as connection:
            values = {row["name"]: int(row["value"]) for row in connection.execute("SELECT * FROM cache_stats")}
            entries = connection.execute(
                "SELECT count(*) AS count FROM cache_entries"
            ).fetchone()
            stored_bytes = connection.execute(
                "SELECT coalesce(sum(size), 0) AS bytes FROM"
                " (SELECT digest, max(size) AS size FROM cache_entries GROUP BY digest)"
            ).fetchone()["bytes"]
        hits = values.get("hits", 0)
        misses = values.get("misses", 0)
        total = hits + misses
        rate = hits / total if total else 0.0
        return "\n".join(
            (
                "# HELP cindral_cache_lookups_total Cache lookup requests by result.",
                "# TYPE cindral_cache_lookups_total counter",
                f'cindral_cache_lookups_total{{result="hit"}} {hits}',
                f'cindral_cache_lookups_total{{result="miss"}} {misses}',
                "# HELP cindral_cache_hit_ratio Fraction of cache lookups that hit.",
                "# TYPE cindral_cache_hit_ratio gauge",
                f"cindral_cache_hit_ratio {rate}",
                "# HELP cindral_cache_entries Number of cache entries.",
                "# TYPE cindral_cache_entries gauge",
                f"cindral_cache_entries {int(entries['count'])}",
                "# HELP cindral_cache_bytes Stored cache-entry bytes.",
                "# TYPE cindral_cache_bytes gauge",
                f"cindral_cache_bytes {int(stored_bytes)}",
                "# HELP cindral_cache_bytes_written_total Bytes uploaded to the cache.",
                "# TYPE cindral_cache_bytes_written_total counter",
                f"cindral_cache_bytes_written_total {values.get('bytes_written', 0)}",
                "",
            )
        )

    def _expire(self, connection: sqlite3.Connection, now: float) -> None:
        connection.execute("DELETE FROM cache_entries WHERE accessed_at < ?", (now - self.ttl_seconds,))

    def _enforce_quota(self, connection: sqlite3.Connection, repository: str) -> None:
        self._evict_to_quota(connection, repository, self.repository_quota)
        self._evict_to_quota(connection, None, self.storage_quota)
        self._collect_blobs(connection)

    @staticmethod
    def _evict_to_quota(
        connection: sqlite3.Connection,
        repository: str | None,
        quota: int,
    ) -> None:
        filter_sql = " WHERE repository=?" if repository is not None else ""
        parameters = (repository,) if repository is not None else ()
        while True:
            total = connection.execute(
                "SELECT coalesce(sum(size), 0) AS bytes FROM"
                " (SELECT digest, max(size) AS size FROM cache_entries"
                f"{filter_sql} GROUP BY digest)",
                parameters,
            ).fetchone()["bytes"]
            if int(total) <= quota:
                return
            oldest = connection.execute(
                "SELECT repository, branch, architecture, key FROM cache_entries"
                f"{filter_sql} ORDER BY accessed_at, created_at, key LIMIT 1",
                parameters,
            ).fetchone()
            if oldest is None:
                return
            connection.execute(
                "DELETE FROM cache_entries WHERE repository=? AND branch=? AND architecture=? AND key=?",
                (oldest["repository"], oldest["branch"], oldest["architecture"], oldest["key"]),
            )

    def _collect_blobs(self, connection: sqlite3.Connection) -> None:
        referenced = {row[0] for row in connection.execute("SELECT DISTINCT digest FROM cache_entries")}
        for directory in self.blobs.iterdir():
            if not directory.is_dir() or directory.is_symlink() or not re.fullmatch(r"[a-f0-9]{2}", directory.name):
                continue
            for blob in directory.iterdir():
                digest = f"{directory.name}{blob.name}"
                if _DIGEST.fullmatch(digest) and digest not in referenced:
                    blob.unlink(missing_ok=True)
            try:
                directory.rmdir()
            except OSError:
                pass

    @staticmethod
    def _entry(row: sqlite3.Row) -> CacheEntry:
        return CacheEntry(
            repository=row["repository"],
            branch=row["branch"],
            architecture=row["architecture"],
            key=row["key"],
            digest=row["digest"],
            size=int(row["size"]),
            created_at=float(row["created_at"]),
        )

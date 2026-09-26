import hashlib
import io
import tempfile
import unittest
from pathlib import Path

from cindral.cache import CacheError, CacheStore


class CacheStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = CacheStore(Path(self.tmp.name) / "cache", ttl_seconds=100, repository_quota=12)

    def put(self, key: str, value: bytes, now: float = 10, branch: str = "main", architecture: str = "arm64"):
        return self.store.put("owner/repo", branch, architecture, key, io.BytesIO(value), len(value), now)

    def test_stores_content_by_digest_and_restores_an_exact_key(self) -> None:
        entry, created = self.put("pnpm-arm64-lock1", b"package-data")
        self.assertTrue(created)
        self.assertEqual(entry.digest, hashlib.sha256(b"package-data").hexdigest())

        hit = self.store.lookup("owner/repo", "main", "arm64", "pnpm-arm64-lock1", now=11)
        self.assertEqual(hit, entry)
        path, size = self.store.open_blob(entry.digest, "owner/repo", "main", "arm64", entry.key, now=11)
        with path:
            self.assertEqual(path.read(), b"package-data")
        self.assertEqual(size, len(b"package-data"))

    def test_cache_key_is_immutable(self) -> None:
        first, created = self.put("key", b"first")
        second, overwritten = self.put("key", b"other")
        self.assertTrue(created)
        self.assertFalse(overwritten)
        self.assertEqual(second.digest, first.digest)
        self.assertEqual(list((Path(self.tmp.name) / "cache").glob("upload-*")), [])

    def test_put_replaces_an_entry_that_has_expired(self) -> None:
        store = CacheStore(Path(self.tmp.name) / "expiry", ttl_seconds=2)
        old, _ = store.put("owner/repo", "main", "arm64", "key", io.BytesIO(b"old"), 3, now=1)
        new, created = store.put("owner/repo", "main", "arm64", "key", io.BytesIO(b"new"), 3, now=4)
        self.assertTrue(created)
        self.assertNotEqual(old.digest, new.digest)

    def test_restore_prefix_stays_within_branch_and_architecture(self) -> None:
        self.put("pnpm-arm64-old", b"data")
        found = self.store.lookup(
            "owner/repo", "main", "arm64", "pnpm-arm64-new", ("pnpm-arm64-",), now=11
        )
        self.assertEqual(found.key, "pnpm-arm64-old")
        self.assertIsNone(self.store.lookup("owner/repo", "release", "arm64", "pnpm-arm64-new", ("pnpm-arm64-",), now=11))
        self.assertIsNone(self.store.lookup("owner/repo", "main", "amd64", "pnpm-amd64-new", ("pnpm-amd64-",), now=11))

    def test_lru_quota_removes_old_entries_and_expired_entries(self) -> None:
        store = CacheStore(Path(self.tmp.name) / "small", ttl_seconds=5, repository_quota=8)
        store.put("owner/repo", "main", "arm64", "old", io.BytesIO(b"12345678"), 8, now=1)
        store.put("owner/repo", "main", "arm64", "new", io.BytesIO(b"abcdefgh"), 8, now=4)
        self.assertIsNone(store.lookup("owner/repo", "main", "arm64", "old", now=4))
        self.assertIsNotNone(store.lookup("owner/repo", "main", "arm64", "new", now=4))
        self.assertIsNone(store.lookup("owner/repo", "main", "arm64", "new", now=10))
        count = next(line for line in store.metrics().splitlines() if line.startswith("cindral_cache_entries "))
        self.assertEqual(count, "cindral_cache_entries 0")

    def test_same_blob_is_deduplicated_across_cache_keys(self) -> None:
        self.put("one", b"same")
        self.put("two", b"same")
        self.assertIn("cindral_cache_bytes 4\n", self.store.metrics())

    def test_global_lru_quota_evicts_oldest_repository_entry(self) -> None:
        store = CacheStore(
            Path(self.tmp.name) / "global",
            ttl_seconds=100,
            repository_quota=8,
            storage_quota=8,
            max_blob_bytes=8,
        )
        store.put("owner/first", "main", "arm64", "first", io.BytesIO(b"12345678"), 8, now=1)
        store.put("owner/second", "main", "arm64", "second", io.BytesIO(b"abcdefgh"), 8, now=2)
        self.assertIsNone(store.lookup("owner/first", "main", "arm64", "first", now=3))
        self.assertIsNotNone(store.lookup("owner/second", "main", "arm64", "second", now=3))

    def test_rejects_invalid_scope_and_oversized_blobs(self) -> None:
        limited = CacheStore(
            Path(self.tmp.name) / "limited",
            ttl_seconds=100,
            repository_quota=20,
            storage_quota=30,
            max_blob_bytes=12,
        )
        with self.assertRaises(CacheError):
            self.store.lookup("../repo", "main", "arm64", "key")
        with self.assertRaises(CacheError):
            self.store.lookup("owner/repo", "main", "arm64", "bad key")
        with self.assertRaises(CacheError):
            limited.put("owner/repo", "main", "arm64", "large", io.BytesIO(b"x" * 13), 13)

    def test_short_upload_does_not_create_an_entry(self) -> None:
        with self.assertRaises(CacheError):
            self.store.put("owner/repo", "main", "arm64", "short", io.BytesIO(b"x"), 2)
        self.assertIsNone(self.store.lookup("owner/repo", "main", "arm64", "short"))

    def test_metrics_include_hits_misses_and_hit_ratio(self) -> None:
        self.put("present", b"data")
        self.store.lookup("owner/repo", "main", "arm64", "present", now=11)
        self.store.lookup("owner/repo", "main", "arm64", "missing", now=11)
        text = self.store.metrics()
        self.assertIn('cindral_cache_lookups_total{result="hit"} 1', text)
        self.assertIn('cindral_cache_lookups_total{result="miss"} 1', text)
        self.assertIn("cindral_cache_hit_ratio 0.5", text)


if __name__ == "__main__":
    unittest.main()

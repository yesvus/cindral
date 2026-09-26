import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from cindral.cache_adapters import (
    CacheArchiveError,
    create_archive,
    detect_cache_plans,
    restore_archive,
    restore_caches,
)


class CacheAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_detects_tool_caches_with_lockfile_and_architecture_keys(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        (repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        (repo / "package.json").write_text('{"dependencies":{"next":"16.0.0"}}')
        (repo / "uv.lock").write_text("version = 1\n")
        (repo / "go.sum").write_text("example.org/mod v1.0.0 h1:abc\n")

        plans = detect_cache_plans(repo, self.root / "cache", "arm64")

        self.assertEqual({plan.name for plan in plans}, {"pnpm", "uv", "go", "next"})
        self.assertTrue(all("arm64" in plan.key for plan in plans))
        self.assertEqual(dict(next(plan for plan in plans if plan.name == "pnpm").environment)["npm_config_store_dir"], "/cindral-cache/pnpm")

    def test_lockfile_content_changes_the_cache_key(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        lock = repo / "pnpm-lock.yaml"
        lock.write_text("first\n")
        first = detect_cache_plans(repo, self.root / "cache", "arm64")[0].key
        lock.write_text("second\n")
        second = detect_cache_plans(repo, self.root / "cache", "arm64")[0].key
        self.assertNotEqual(first, second)

    def test_ignores_lockfiles_that_are_symlinks_outside_the_checkout(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        external = self.root / "outside-lock"
        external.write_text("secret material")
        (repo / "pnpm-lock.yaml").symlink_to(external)
        self.assertEqual(detect_cache_plans(repo, self.root / "cache", "arm64"), ())

    def test_skips_cache_paths_with_symlinked_ancestors(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        (repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        (repo / "package.json").write_text('{"dependencies":{"next":"16.0.0"}}')
        outside = self.root / "outside"
        outside.mkdir()
        (repo / ".next").symlink_to(outside)
        plans = detect_cache_plans(repo, self.root / "cache", "arm64")
        lines = io.StringIO()

        restore_caches(None, None, plans, self.root, lines)

        self.assertFalse((outside / "cache").exists())
        self.assertIn("cache directory skipped for next", lines.getvalue())

    def test_archives_cache_files_without_following_symlinks(self) -> None:
        cache = self.root / "cache"
        cache.mkdir()
        (cache / "package.data").write_text("content")
        secret = self.root / "secret"
        secret.write_text("host data")
        (cache / "external").symlink_to(secret)
        archive = self.root / "cache.tar.gz"

        self.assertTrue(create_archive(cache, archive))
        restored = self.root / "restored"
        restore_archive(archive, restored)
        self.assertEqual((restored / "package.data").read_text(), "content")
        self.assertFalse((restored / "external").exists())

    def test_rejects_traversal_paths_during_restore(self) -> None:
        archive_path = self.root / "unsafe.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            info = tarfile.TarInfo("../escape")
            info.size = 4
            archive.addfile(info, io.BytesIO(b"evil"))

        with self.assertRaises(CacheArchiveError):
            restore_archive(archive_path, self.root / "restore")
        self.assertFalse((self.root / "escape").exists())


if __name__ == "__main__":
    unittest.main()

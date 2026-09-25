import re
import unittest
from pathlib import Path

from scripts.release import next_version


ROOT = Path(__file__).resolve().parents[1]


class VersionTest(unittest.TestCase):
    def test_version_matches_pyproject(self) -> None:
        version = (ROOT / "VERSION").read_text().strip()
        pyproject = (ROOT / "pyproject.toml").read_text()
        match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
        self.assertIsNotNone(match)
        self.assertEqual(version, f"v{match.group(1)}")

    def test_release_bumps(self) -> None:
        self.assertEqual(next_version("v0.1.0", "patch"), "v0.1.1")
        self.assertEqual(next_version("v0.1.0", "minor"), "v0.2.0")
        self.assertEqual(next_version("v0.1.0", "major"), "v1.0.0")


if __name__ == "__main__":
    unittest.main()

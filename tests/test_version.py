import re
import unittest
from pathlib import Path

from scripts.release import next_version


ROOT = Path(__file__).resolve().parents[1]


class VersionTest(unittest.TestCase):
    def test_the_packaging_version_is_read_from_the_file_not_typed(self) -> None:
        """One source, so there is nothing to keep in step.

        The version used to be typed into VERSION and into pyproject.toml, and a
        test compared the two strings. That catches a mismatch but leaves the
        mechanism that produces one: a release that writes a file and forgets the
        other. Declaring the version dynamic and pointing the build at the file
        removes the second copy, so the failure mode has nothing to happen in.
        """
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertIn('dynamic = ["version"]', pyproject)
        self.assertNotRegex(pyproject, r'(?m)^version\s*=')
        self.assertIn('path = "VERSION"', pyproject)

    def test_a_release_writes_only_the_file_the_version_lives_in(self) -> None:
        """The release script is where drift used to be introduced."""
        release = (ROOT / "scripts" / "release.py").read_text()
        self.assertNotIn("pyproject", release)

    def test_the_built_package_declares_the_file_version(self) -> None:
        """What a build declares and what /healthz reports are the same string.

        The runtime reads VERSION, so the build reading the same file is what makes
        a deployed version meaningful: v0.12.0 in one and 0.11.0 in the other is a
        build nobody can identify.
        """
        import tomllib

        declared = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertNotIn("version", declared["project"])
        pattern = declared["tool"]["hatch"]["version"]["pattern"]
        from_file = (ROOT / "VERSION").read_text().strip()
        match = re.fullmatch(pattern, from_file)
        self.assertIsNotNone(match, f"{pattern!r} does not read VERSION's {from_file!r}")
        self.assertEqual(match.group("version"), from_file.lstrip("v"))

    def test_release_bumps(self) -> None:
        self.assertEqual(next_version("v0.1.0", "patch"), "v0.1.1")
        self.assertEqual(next_version("v0.1.0", "minor"), "v0.2.0")
        self.assertEqual(next_version("v0.1.0", "major"), "v1.0.0")


if __name__ == "__main__":
    unittest.main()

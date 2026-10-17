"""Tests for the build metadata the broker reports."""
import unittest
from unittest.mock import patch

from cindral.build_info import BuildInfo, build_labels, current_build, escape


class CurrentBuildTest(unittest.TestCase):
    def test_build_metadata_is_read_from_the_environment(self) -> None:
        with patch.dict("os.environ", {
            "CINDRAL_VERSION": "0.11.0",
            "CINDRAL_COMMIT": "a" * 40,
            "CINDRAL_BUILT_AT": "2026-09-28T10:00:00Z",
        }, clear=False):
            build = current_build()
        self.assertEqual(build.version, "0.11.0")
        self.assertEqual(build.as_dict()["commit"], "a" * 40)

    def test_an_unset_value_reports_unknown_rather_than_a_blank(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            build = current_build()
        self.assertEqual(build.as_dict(), {"version": "unknown", "commit": "unknown", "built_at": "unknown"})

    def test_the_version_is_never_inferred_from_a_file(self) -> None:
        # a source checkout has no build to describe, and a VERSION file copied
        # into an image would report what the file says rather than what was
        # built, which is the one reading that cannot settle a version question
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(current_build().version, "unknown")


class LabelTest(unittest.TestCase):
    def test_labels_name_the_build(self) -> None:
        labels = build_labels(BuildInfo(version="0.11.0", commit="abc1234"))
        self.assertEqual(labels, 'version="0.11.0",commit="abc1234"')

    def test_a_label_value_cannot_break_out_of_its_quotes(self) -> None:
        labels = build_labels(BuildInfo(version='a"b', commit="c\nd"))
        self.assertEqual(labels, 'version="a\\"b",commit="c d"')

    def test_escaping_neutralizes_backslashes_too(self) -> None:
        self.assertEqual(escape('a\\b'), "a\\\\b")

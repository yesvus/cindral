"""Unit tests for codebase file size limits and quality contracts."""

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_file_limits import check_file_limits, load_config



class TestCodeQuality(unittest.TestCase):
    def test_repository_files_within_line_limits(self) -> None:
        root = pathlib.Path(__file__).resolve().parent.parent
        violations = check_file_limits(root)
        self.assertEqual(violations, [], f"Repository files exceeded line limits: {violations}")

    def test_catches_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            src_dir = root / "src" / "pkg"
            src_dir.mkdir(parents=True)
            oversized_file = src_dir / "large.py"
            oversized_file.write_text("\n".join(f"line_{i} = {i}" for i in range(450)), encoding="utf-8")

            violations = check_file_limits(root)
            self.assertEqual(len(violations), 1)
            self.assertIn("src/pkg/large.py: 450 lines exceeds limit of 400", violations[0])

    def test_respects_baseline_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            src_dir = root / "src" / "pkg"
            src_dir.mkdir(parents=True)
            oversized_file = src_dir / "legacy.py"
            oversized_file.write_text("\n".join(f"line_{i} = {i}" for i in range(450)), encoding="utf-8")

            pyproject = root / "pyproject.toml"
            pyproject.write_text(
                '[tool.cindral.code_quality]\n'
                'max_lines_src = 400\n'
                'baseline_overrides = { "src/pkg/legacy.py" = 500 }\n',
                encoding="utf-8",
            )

            violations = check_file_limits(root)
            self.assertEqual(violations, [])

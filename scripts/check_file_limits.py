"""Enforce source and test file line count limits configured in pyproject.toml."""

import argparse
import pathlib
import sys
import tomllib


def load_config(root: pathlib.Path) -> tuple[int, int, dict[str, int]]:
    pyproject_path = root / "pyproject.toml"
    max_lines_src = 400
    max_lines_test = 650
    overrides: dict[str, int] = {}

    if pyproject_path.exists():
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        cfg = data.get("tool", {}).get("cindral", {}).get("code_quality", {})
        max_lines_src = cfg.get("max_lines_src", max_lines_src)
        max_lines_test = cfg.get("max_lines_test", max_lines_test)
        overrides = cfg.get("baseline_overrides", overrides)

    return max_lines_src, max_lines_test, overrides


def check_file_limits(root: pathlib.Path) -> list[str]:
    max_lines_src, max_lines_test, overrides = load_config(root)
    violations: list[str] = []

    for path in sorted((root / "src").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        limit = overrides.get(rel, max_lines_src)
        count = len(path.read_text(encoding="utf-8").splitlines())
        if count > limit:
            violations.append(f"{rel}: {count} lines exceeds limit of {limit}")

    for path in sorted((root / "tests").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        limit = overrides.get(rel, max_lines_test)
        count = len(path.read_text(encoding="utf-8").splitlines())
        if count > limit:
            violations.append(f"{rel}: {count} lines exceeds limit of {limit}")

    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parent.parent,
        help="Repository root directory",
    )
    args = parser.parse_args()

    violations = check_file_limits(args.root)
    if violations:
        sys.stderr.write("File line limit violations detected:\n")
        for item in violations:
            sys.stderr.write(f"  - {item}\n")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

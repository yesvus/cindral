"""Prepare a Cindral version bump without pushing."""
import argparse
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def next_version(current: str, bump: str) -> str:
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", current)
    if not match:
        raise ValueError(f"invalid VERSION: {current}")
    major, minor, patch = map(int, match.groups())
    if bump == "major":
        major, minor, patch = major + 1, 0, 0
    elif bump == "minor":
        minor, patch = minor + 1, 0
    elif bump == "patch":
        patch += 1
    else:
        raise ValueError(f"invalid bump: {bump}")
    return f"v{major}.{minor}.{patch}"


def update_files(version: str) -> None:
    """Write the one file the version lives in.

    Only VERSION. The packaging metadata reads this file rather than carrying its
    own copy, because two files that must be edited together did drift: three
    releases shipped with the runtime reporting one version and the built package
    another, and nothing caught it.
    """
    (ROOT / "VERSION").write_text(f"{version}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bump", choices=("major", "minor", "patch"))
    parser.add_argument("--write", action="store_true", help="update VERSION")
    args = parser.parse_args()
    current = (ROOT / "VERSION").read_text().strip()
    version = next_version(current, args.bump)
    print(f"release: {current} -> {version}")
    if args.write:
        update_files(version)
        print(f"updated VERSION to {version}")
    else:
        print("dry-run: pass --write to update files")


if __name__ == "__main__":
    main()

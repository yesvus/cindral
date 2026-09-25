"""Prepare a Runner Relay version bump without pushing."""
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
    (ROOT / "VERSION").write_text(f"{version}\n")
    pyproject = ROOT / "pyproject.toml"
    content = pyproject.read_text()
    updated, count = re.subn(r'^version\s*=\s*"[^"]+"', f'version = "{version[1:]}"', content, count=1, flags=re.M)
    if count != 1:
        raise ValueError("pyproject version line not found")
    pyproject.write_text(updated)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bump", choices=("major", "minor", "patch"))
    parser.add_argument("--write", action="store_true", help="update VERSION and pyproject.toml")
    args = parser.parse_args()
    current = (ROOT / "VERSION").read_text().strip()
    version = next_version(current, args.bump)
    print(f"release: {current} -> {version}")
    if args.write:
        update_files(version)
        print(f"updated VERSION and pyproject.toml to {version[1:]}")
    else:
        print("dry-run: pass --write to update files")


if __name__ == "__main__":
    main()

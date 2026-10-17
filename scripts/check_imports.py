"""Import every module in the package, so an import-order fault is caught here.

A forward reference written as a bare annotation is a runtime name lookup, not
a string. A newer interpreter defers annotation evaluation and a project that
declares a lower minimum does not, so a module can pass every test on the
newest interpreter and raise NameError on the version a device actually runs.
This runs the import on the running interpreter and on the declared minimum, so
that gap is a red check rather than a failed job on someone else's hardware.

An interpreter that is not available is reported and skipped rather than
failing, because a device without network access cannot fetch one.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
#: Used only when pyproject cannot be read, and it must agree with it. A fallback
#: that names a different interpreter is a second declaration of the floor, and two
#: declarations drift.
FALLBACK_MINIMUM = "3.12"


def declared_minimum() -> str:
    """The lower bound in pyproject, so this and the declaration cannot drift."""
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("requires-python"):
            for part in line.split("=", 1)[1].split(","):
                part = part.strip().strip("\"'")
                if part.startswith(">="):
                    return part[2:].strip()
    return FALLBACK_MINIMUM


def modules() -> list[str]:
    package = ROOT / "src" / "cindral"
    return sorted(f"cindral.{path.stem}" for path in package.glob("*.py") if path.stem != "__init__")


def import_all(command: list[str]) -> int:
    script = "".join(f"import {name}\n" for name in modules())
    return subprocess.run(command + ["-c", script], cwd=ROOT, check=False).returncode


def main() -> int:
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    minimum = declared_minimum()
    failed = False
    for label, command in (
        (f"running interpreter ({running})", [sys.executable]),
        (f"declared minimum ({minimum})", ["uv", "run", "--quiet", "--python", minimum, "python"]),
    ):
        if command[0] == "uv" and minimum == running:
            continue
        print(f"==> module import on {label}", flush=True)
        try:
            if import_all(command):
                failed = True
        except FileNotFoundError:
            print(f"    skipped: {command[0]} is not available here")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

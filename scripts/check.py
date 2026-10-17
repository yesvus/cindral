"""Run Cindral's Python quality checks."""

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from check_imports import declared_minimum  # noqa: E402 - the path is set above it

ROOT = pathlib.Path(__file__).resolve().parent.parent
MINIMUM = declared_minimum()
RUNNING = f"{sys.version_info.major}.{sys.version_info.minor}"

CHECKS = (
    ("file-size limits", (sys.executable, "scripts/check_file_limits.py")),
    ("unit tests", (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v")),
    ("bytecode compilation", (sys.executable, "-m", "compileall", "-q", "src")),
    ("module import", (sys.executable, "scripts/check_imports.py")),
)


def minimum_suite() -> tuple[str, list[str]] | None:
    """The unit tests again on the declared minimum, when this is not it.

    A declared lower bound that nothing runs does not mean anything, and the import
    check is the only thing exercising it today: imports pass on a floor where a
    call, a match or a comprehension behaves differently, and the tests would have
    said so had they run there. They ran here instead because the resolver happened
    to pick the floor, which is an accident that a lockfile update ends silently.

    So when this interpreter is newer than the floor, the suite runs on the floor as
    well. When it is the floor already, the run above *is* the floor run and nothing
    is repeated.
    """
    if RUNNING == MINIMUM:
        return None
    return (
        f"unit tests on the declared minimum ({MINIMUM})",
        [
            "uv", "run", "--quiet", "--python", MINIMUM, "python",
            "-m", "unittest", "discover", "-s", "tests", "-v",
        ],
    )


def main() -> int:
    # Named in the output because the whole point is that a reader can tell which
    # interpreter answered. A gate that ran the suite somewhere and said nothing is
    # how a 3.14-only module reaches a 3.12 device with every check green.
    print(f"==> interpreter: {RUNNING} (declared minimum {MINIMUM})", flush=True)
    extra = minimum_suite()
    if extra is None:
        print(f"    the floor is this interpreter: {MINIMUM} is exercised above")
    else:
        print(f"    {RUNNING} is newer than the floor, so the suite repeats on {MINIMUM}")
    for name, command in CHECKS:
        print(f"==> {name}", flush=True)
        result = subprocess.run(command, cwd=ROOT, check=False)
        if result.returncode:
            return result.returncode
    if extra is not None:
        name, command = extra
        print(f"==> {name}", flush=True)
        try:
            result = subprocess.run(command, cwd=ROOT, check=False)
        except FileNotFoundError:
            # Loud on purpose. A skipped floor run that only says "skipped" is the
            # same silence this exists to end, so it is named as unexercised.
            print(f"    WARNING: uv is unavailable, so {MINIMUM} was NOT exercised", flush=True)
            return 1
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())

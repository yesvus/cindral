"""Argparse types for the values a device and a job are configured with.

Each one refuses a value that would misbehave quietly rather than loudly, and
each says what the ceiling is for, because a limit nobody can explain is a limit
the next person removes.
"""
import argparse
import math
import os
import re

from .agent import MAX_CONCURRENCY, MAX_HEARTBEAT_SECONDS, MIN_IDLE_SECONDS, ONLINE_WINDOW_SECONDS
from .executor import MIN_JOB_CPUS


def byte_rate(value: str) -> int:
    """Argparse type for a block-I/O cap in bytes per second.

    Separate from `positive_int`, which is bounded by the concurrency ceiling
    because it counts slots. A rate is a different quantity: reusing the slot
    parser would refuse every real value, a 1 MB/s cap among them, while accepting
    the handful small enough to look like a slot count.
    """
    try:
        rate = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number of bytes") from None
    if rate <= 0:
        # zero would be read as "no limit" by every layer below it, so it is
        # refused here rather than turning into an uncapped device that looks capped
        raise argparse.ArgumentTypeError(
            f"{value!r} must be greater than zero bytes per second; leave the flag unset"
            " for no cap"
        )
    return rate


def heartbeat_interval(value: str) -> float:
    """Argparse type for the interval, kept inside the broker's online window.

    A device that beats slower than the broker's window would be dropped from
    the pool between beats, so it would read as absent while it is working.
    """
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_HEARTBEAT_SECONDS:
        raise argparse.ArgumentTypeError(
            f"{value!r} must be a finite number greater than zero and at most "
            f"{MAX_HEARTBEAT_SECONDS:g}, so a device stays inside the broker's "
            f"{ONLINE_WINDOW_SECONDS:g}s online window"
        )
    return seconds


def positive_int(value: str) -> int:
    """Argparse type for a count of slots."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number") from None
    if not 1 <= number <= MAX_CONCURRENCY:
        raise argparse.ArgumentTypeError(f"{value!r} must be between 1 and {MAX_CONCURRENCY}")
    return number


def positive_number(value: str) -> float:
    """Argparse type for an interval or a cap, which may be fractional.

    `nan` and `inf` are refused: both satisfy a bare positivity test, and a
    non-finite value reaches `Event.wait` or docker as an unusable flag rather
    than as a working interval or limit.
    """
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
    if not number > 0 or not math.isfinite(number):
        raise argparse.ArgumentTypeError(f"{value!r} must be a finite number greater than zero")
    return number


def idle_interval(value: str) -> float:
    """Argparse type for the wait between empty claims, which has a floor.

    A shorter wait than the floor turns an empty queue into a claim request per
    slot per wait, which loads the broker and burns the device's own cpu.
    """
    seconds = positive_number(value)
    if seconds < MIN_IDLE_SECONDS:
        raise argparse.ArgumentTypeError(
            f"{value!r} is too short, the idle wait must be at least {MIN_IDLE_SECONDS} seconds"
        )
    return seconds


def cpu_limit(value: str) -> float:
    """Argparse type for a per-job cpu cap, which has a floor.

    Below the floor the quota rounds to a fraction of a millisecond of cpu per
    scheduling period, so the job container makes no progress.
    """
    cpus = positive_number(value)
    if cpus < MIN_JOB_CPUS:
        raise argparse.ArgumentTypeError(
            f"{value!r} is too small to run anything, the cpu cap must be at least {MIN_JOB_CPUS}"
        )
    return cpus


MEMORY_SIZE = re.compile(r"(\d+(?:\.\d+)?)[bkmgt](?:i?b)?", re.IGNORECASE)


def capacity_option(name: str, default: str | None = None) -> str | None:
    """Reads a capacity option from the environment, treating blank as unset.

    An empty variable is how a unit file, compose file, or shell exports "no
    value here", so it must fall back to the default rather than reach a
    converter as an empty string.
    """
    value = os.environ.get(name, "").strip()
    return value or default


def memory_size(value: str) -> str:
    """Argparse type for a Docker memory size, passed through to --memory.

    A unit is required and no space is allowed inside: a bare number would reach
    Docker as bytes, where `12` reads as twelve bytes, and `12 g` is passed on
    verbatim, so the failure lands on the job rather than on the operator.
    """
    size = value.strip()
    match = MEMORY_SIZE.fullmatch(size)
    if match is None or float(match.group(1)) == 0:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a docker memory size, use a positive value with a unit like 12g, 512m, or 2GiB"
        )
    return size

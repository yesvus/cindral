"""What caps a job runs under, and how each docker surface spells them.

One place, because the caps reach three surfaces that do not agree: the job
container, the build container, and the line an operator reads when the agent
starts. A cap that is honoured on one and dropped on another is a limit that reads
as configured and is not, which is the failure this module exists to prevent.
"""

#: Block-I/O caps reach `docker run` and nothing else.
#:
#: BuildKit's `--resource` is a container resource spec and takes only cpu and
#: memory. Passing a disk key is refused, not ignored:
#:
#:     $ docker buildx build --resource device-write-bps=1048576 .
#:     ERROR: failed to build: unknown resource "device-write-bps"
#:
#: So the build path says the cap does not reach it rather than emitting a flag that
#: fails every build, and the job container, which is where a build's writes land
#: afterwards, carries it.
BUILD_SUPPORTS_DISK = False


class JobLimits:
    """The caps one device applies to every job it runs."""

    def __init__(
        self,
        cpus: float | None = None,
        memory: str | None = None,
        disk_write: int | None = None,
        disk_read: int | None = None,
    ) -> None:
        for name, value in (("disk read", disk_read), ("disk write", disk_write)):
            if value is not None and value <= 0:
                raise ValueError(f"a {name} cap must be positive bytes per second, not {value}")
        self.cpus = cpus
        self.memory = memory
        self.disk_write = disk_write
        self.disk_read = disk_read

    @property
    def capped(self) -> bool:
        return any(
            value is not None
            for value in (self.cpus, self.memory, self.disk_write, self.disk_read)
        )

    def describe(self) -> str:
        """One line naming the caps, or saying plainly that there are none.

        Read when the agent starts rather than from a job log. An uncapped device
        was previously only visible by finding the word "uncapped" inside the log of
        a build it happened to run, which is not a thing an operator does before
        pointing a stream of jobs at a host.
        """
        parts: list[str] = []
        if self.cpus is not None:
            parts.append(f"cpu={self.cpus:g}")
        if self.memory is not None:
            parts.append(f"memory={self.memory}")
        if self.disk_write is not None:
            parts.append(f"disk-write={self.disk_write}B/s")
        if self.disk_read is not None:
            parts.append(f"disk-read={self.disk_read}B/s")
        if not parts:
            return (
                "no job limits: cpu, memory, disk read and disk write are all uncapped,"
                " so a stream of jobs can saturate this host's disk and memory"
            )
        missing = [
            name
            for name, value in (
                ("cpu", self.cpus), ("memory", self.memory),
                ("disk-write", self.disk_write), ("disk-read", self.disk_read),
            )
            if value is None
        ]
        line = "job limits: " + ", ".join(parts)
        if missing:
            line += f" (uncapped: {', '.join(missing)})"
        return line

    def run_args(self) -> list[str]:
        """The caps as `docker run` flags.

        A cpu cap becomes a quota inside one CFS period, and swap is pinned to
        memory so the memory cap is the whole budget rather than RAM alone.
        """
        values: list[tuple[str, str]] = []
        if self.cpus is not None:
            values += [("cpu-period", "100000"), ("cpu-quota", str(int(self.cpus * 100_000)))]
        if self.memory is not None:
            values += [("memory", self.memory), ("memory-swap", self.memory)]
        if self.disk_write is not None:
            values.append(("device-write-bps", str(self.disk_write)))
        if self.disk_read is not None:
            values.append(("device-read-bps", str(self.disk_read)))
        return [flag for key, value in values for flag in (f"--{key}", value)]

    def build_args(self, resource: bool) -> list[str]:
        """The caps as `docker build` accepts them, which is a subset.

        BuildKit spells them as repeatable `--resource key=value` and the legacy
        builder takes the run flags. Disk is absent from the build path for the
        reason on `BUILD_SUPPORTS_DISK`, so it is left to the job container.
        """
        values: list[tuple[str, str]] = []
        if self.cpus is not None:
            values += [("cpu-period", "100000"), ("cpu-quota", str(int(self.cpus * 100_000)))]
        if self.memory is not None:
            values += [("memory", self.memory), ("memory-swap", self.memory)]
        if resource:
            return [flag for key, value in values for flag in ("--resource", f"{key}={value}")]
        return [flag for key, value in values for flag in (f"--{key}", value)]

    def build_disk_note(self) -> str | None:
        """What to say when a disk cap cannot reach the build, or None if none is set."""
        if BUILD_SUPPORTS_DISK or (self.disk_write is None and self.disk_read is None):
            return None
        sides = ", ".join(
            side for side, value in (("write", self.disk_write), ("read", self.disk_read))
            if value is not None
        )
        return (
            f"cindral: warning: the disk {sides} cap does not reach the build container:"
            " docker build has no block-I/O resource flag, so it applies to the job"
            " container and to nothing before it"
        )

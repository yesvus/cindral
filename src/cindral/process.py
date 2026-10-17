"""Running local processes for the device executor.

Split out of the executor because neither helper is about jobs: a caller that
wants a command's exit code with its output in a log, or a bounded tail of a
log, does not need the Docker executor that happens to use them.
"""
import subprocess
import threading
from typing import TextIO

MAX_LOG_CHARS = 64 * 1024


class ExecutorError(RuntimeError):
    """Raised when the device cannot prepare or run a job."""


def tail(text: str, limit: int = MAX_LOG_CHARS) -> str:
    if len(text) <= limit:
        return text
    return "...(truncated)...\n" + text[-limit:]


class ProcessRunner:
    """Runs local processes, streaming their output into a log file."""

    def run(
        self,
        argv: list[str],
        log: TextIO,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        try:
            process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                env=env,
                cwd=cwd,
                bufsize=1,
            )
        except OSError as exc:
            log.write(f"cannot start {argv[0]}: {exc}\n")
            return 127
        stopped = threading.Event()

        def watch() -> None:
            while cancel is not None and not stopped.wait(1):
                if cancel.is_set():
                    process.kill()
                    return

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    log.write(line)
            process.wait()
        finally:
            stopped.set()
            process.kill()
            process.wait()
        return process.returncode

    def check(
        self,
        argv: list[str],
        log: TextIO,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        code = self.run(argv, log, env=env, cwd=cwd, cancel=cancel)
        if code != 0:
            raise ExecutorError(f"command failed (exit {code}): {' '.join(argv)}")

    def capture(self, argv: list[str]) -> tuple[int, str]:
        """Runs a short command and returns its exit code and output."""
        try:
            completed = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True,
            )
        except OSError as exc:
            return 127, str(exc)
        return completed.returncode, completed.stdout or ""

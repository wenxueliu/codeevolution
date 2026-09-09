"""Cancelable process-group runner for CodeGraph init/sync commands."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    cancelled: bool = False
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and not self.cancelled and not self.timed_out


class CodeGraphCommandRunner:
    def __init__(
        self,
        executable: str = "codegraph",
        timeout_seconds: float = 900,
        terminate_grace_seconds: float = 5,
    ):
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.terminate_grace_seconds = terminate_grace_seconds

    def init_or_sync(self, repo_path: str | Path, cancellation: Any = None) -> CommandResult:
        root = Path(repo_path).expanduser().resolve()
        action = "sync" if (root / ".codegraph" / "codegraph.db").is_file() else "init"
        return self.run((self.executable, action), root, cancellation)

    def run(
        self, argv: Sequence[str], cwd: str | Path, cancellation: Any = None
    ) -> CommandResult:
        if not argv or any(not isinstance(value, str) or "\x00" in value for value in argv):
            raise ValueError("command argv must contain safe strings")
        root = Path(cwd).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("command working directory does not exist")

        started = time.monotonic()
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                list(argv), cwd=root, stdin=subprocess.DEVNULL, stdout=stdout_file,
                stderr=stderr_file, start_new_session=True,
            )
            cancelled = False
            timed_out = False
            while process.poll() is None:
                if _cancel_requested(cancellation):
                    cancelled = True
                    self._terminate(process)
                    break
                if time.monotonic() - started >= self.timeout_seconds:
                    timed_out = True
                    self._terminate(process)
                    break
                time.sleep(0.05)
            process.wait()
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read().decode("utf-8", errors="replace")
            stderr = stderr_file.read().decode("utf-8", errors="replace")
        return CommandResult(
            argv=tuple(argv), returncode=process.returncode, stdout=stdout, stderr=stderr,
            duration_seconds=time.monotonic() - started, cancelled=cancelled, timed_out=timed_out,
        )

    def _terminate(self, process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=self.terminate_grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def _cancel_requested(cancellation: Any) -> bool:
    if cancellation is None:
        return False
    if callable(cancellation):
        return bool(cancellation())
    checker = getattr(cancellation, "is_set", None)
    if callable(checker):
        return bool(checker())
    return bool(getattr(cancellation, "cancelled", False))

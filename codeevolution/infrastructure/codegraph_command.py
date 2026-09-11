"""Cancelable process-group runner for CodeGraph init/sync commands."""

from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..platform import PlatformCapabilityError, resolve_executable, start_process, terminate_process


class CodeGraphTerminationError(RuntimeError):
    """CodeGraph could not be terminated together with its process tree."""


class CodeGraphPreflightError(RuntimeError):
    """The resolved CodeGraph executable failed its version preflight."""


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
        executable: str | Sequence[str] = "codegraph",
        timeout_seconds: float = 900,
        terminate_grace_seconds: float = 5,
    ):
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.terminate_grace_seconds = terminate_grace_seconds
        self._verified_executables: set[str] = set()

    def init_or_sync(self, repo_path: str | Path, cancellation: Any = None) -> CommandResult:
        root = Path(repo_path).expanduser().resolve()
        action = "sync" if (root / ".codegraph" / "codegraph.db").is_file() else "init"
        prefix = (self.executable,) if isinstance(self.executable, str) else tuple(self.executable)
        return self.run((*prefix, action), root, cancellation)

    def run(
        self, argv: Sequence[str], cwd: str | Path, cancellation: Any = None
    ) -> CommandResult:
        if not argv or any(not isinstance(value, str) or "\x00" in value for value in argv):
            raise ValueError("command argv must contain safe strings")
        root = Path(cwd).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("command working directory does not exist")

        resolved = resolve_executable(argv[0])
        command = (resolved, *argv[1:])
        if (
            isinstance(self.executable, str)
            and _is_codegraph_executable(resolved)
            and resolved not in self._verified_executables
        ):
            version = self._execute(
                (resolved, "--version"), root, cancellation, min(self.timeout_seconds, 30.0)
            )
            if version.cancelled:
                return CommandResult(
                    argv=command,
                    returncode=version.returncode,
                    stdout=version.stdout,
                    stderr=version.stderr,
                    duration_seconds=version.duration_seconds,
                    cancelled=True,
                )
            if version.timed_out:
                raise CodeGraphPreflightError("CodeGraph version preflight timed out")
            if not version.succeeded:
                detail = version.stderr.strip() or version.stdout.strip() or "unknown error"
                raise CodeGraphPreflightError(
                    f"CodeGraph version preflight failed: {detail[:300]}"
                )
            self._verified_executables.add(resolved)
        return self._execute(command, root, cancellation, self.timeout_seconds)

    def _execute(
        self,
        command: Sequence[str],
        root: Path,
        cancellation: Any,
        timeout_seconds: float,
    ) -> CommandResult:
        started = time.monotonic()
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = start_process(
                list(command), cwd=root, stdout=stdout_file, stderr=stderr_file,
                new_process_group=True,
                no_window=True,
            )
            cancelled = False
            timed_out = False
            while process.poll() is None:
                if _cancel_requested(cancellation):
                    cancelled = True
                    self._terminate(process)
                    break
                if time.monotonic() - started >= timeout_seconds:
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
            argv=tuple(command), returncode=process.returncode, stdout=stdout, stderr=stderr,
            duration_seconds=time.monotonic() - started, cancelled=cancelled, timed_out=timed_out,
        )

    def _terminate(self, process: subprocess.Popen[bytes]) -> None:
        try:
            terminate_process(process, tree=True, grace_seconds=self.terminate_grace_seconds)
        except (OSError, PlatformCapabilityError) as error:
            raise CodeGraphTerminationError(
                f"unable to terminate CodeGraph process tree (pid={process.pid})"
            ) from error


def _cancel_requested(cancellation: Any) -> bool:
    if cancellation is None:
        return False
    if callable(cancellation):
        return bool(cancellation())
    checker = getattr(cancellation, "is_set", None)
    if callable(checker):
        return bool(checker())
    return bool(getattr(cancellation, "cancelled", False))


def _is_codegraph_executable(path: str) -> bool:
    """Only preflight the real CodeGraph CLI, not injected test commands."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.removesuffix(".cmd").removesuffix(".exe") == "codegraph"

"""Small, explicit platform boundary used by the service and capture pipeline.

The application deals in paths, commands and process handles.  The details of
Windows process trees, directory flushing and pathname namespaces belong here
so that a POSIX-only primitive cannot accidentally leak into a business use
case.
"""

from __future__ import annotations

import errno
import ntpath
import os
import shutil
import signal
import stat
import subprocess
import time
import unicodedata
import warnings
from pathlib import Path, PurePosixPath
from typing import IO, Any, Literal


class PlatformCapabilityError(RuntimeError):
    """Raised when a requested filesystem/process guarantee is unavailable."""


def start_process(
    argv: list[str] | tuple[str, ...],
    *,
    cwd: str | Path,
    stdin: Any = subprocess.DEVNULL,
    stdout: Any = subprocess.PIPE,
    stderr: Any = subprocess.PIPE,
    env: dict[str, str] | None = None,
    new_process_group: bool = True,
    no_window: bool = False,
) -> subprocess.Popen:
    """Start a child with the platform's isolated-process defaults."""
    if not argv or any(not isinstance(value, str) or "\x00" in value for value in argv):
        raise ValueError("process argv must contain safe strings")
    options: dict[str, Any] = {
        "cwd": Path(cwd),
        "stdin": stdin,
        "stdout": stdout,
        "stderr": stderr,
        "close_fds": True,
    }
    if env is not None:
        options["env"] = env
    if os.name == "nt":
        flags = 0
        if new_process_group:
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if no_window:
            flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        options["creationflags"] = flags
    else:
        # POSIX process-group isolation is required for tree termination even
        # when the Windows caller only requested CREATE_NO_WINDOW semantics.
        options["start_new_session"] = True
    return subprocess.Popen(list(argv), **options)


def is_reparse_point(path: str | Path) -> bool:
    """Return whether a path is a symlink or Windows reparse point."""
    candidate = Path(path)
    if candidate.is_symlink():
        return True
    try:
        result = candidate.lstat()
    except OSError:
        return False
    return bool(getattr(result, "st_file_attributes", 0) & 0x400)


def _has_reparse_component(path: str | Path) -> bool:
    """Check existing components without resolving through a reparse point."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts:
        if component == absolute.anchor:
            continue
        current /= component
        if is_reparse_point(current):
            return True
    return False


def open_beneath(root: str | Path, relative: PurePosixPath) -> int:
    """Open a regular file beneath ``root`` without traversing links."""
    try:
        relative = PurePosixPath(normalize_manifest_path(relative.as_posix()))
    except (AttributeError, ValueError) as exc:
        raise OSError("unsafe relative source path") from exc
    root_path = Path(root).resolve()
    if os.name == "nt":
        candidate = root_path.joinpath(*relative.parts)
        current = root_path
        for part in relative.parts:
            current = current / part
            if is_reparse_point(current):
                raise OSError("reparse point in source path")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root_path)
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError("source is not a regular file")
        return fd

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    current_fd = os.open(root_path, directory_flags)
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return os.open(relative.parts[-1], os.O_RDONLY | nofollow, dir_fd=current_fd)
    finally:
        os.close(current_fd)


def resolve_executable(name: str) -> str:
    """Resolve an executable without going through a shell.

    npm installs a ``.cmd`` shim on Windows.  Looking for it explicitly keeps
    command resolution consistent for the CLI, API and background workers.
    """
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError("executable name must be a non-empty string")
    candidates = (name,)
    if os.name == "nt" and not Path(name).suffix:
        candidates = (f"{name}.cmd", f"{name}.exe", name)
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            # Preserve an explicitly supplied path (tests and callers may use
            # a symlink intentionally); PATH lookups are made absolute by
            # ``which`` already.
            return os.path.abspath(resolved)
    raise FileNotFoundError(f"executable not found: {name}")


def sqlite_readonly_uri(path: str | Path) -> str:
    """Build a correctly escaped read-only SQLite URI for any local path."""
    resolved = Path(path).expanduser().resolve()
    # Path.as_uri() performs the required escaping for %, #, ?, spaces and
    # Unicode.  Keep the query separate; it must not be percent encoded.
    return f"{resolved.as_uri()}?mode=ro"


def ensure_supported_storage_path(path: str | Path) -> None:
    """Reject Windows volumes whose locking/rename durability is unverified."""
    if os.name != "nt":
        return
    import ctypes

    raw = os.path.abspath(os.fspath(Path(path).expanduser()))
    if raw.startswith("\\\\"):
        raise PlatformCapabilityError(
            "UNC/network storage is not supported; use a local NTFS data directory"
        )
    if _has_reparse_component(raw):
        raise PlatformCapabilityError(
            "reparse-point storage paths are not supported; use a normal local NTFS directory"
        )
    drive, _ = ntpath.splitdrive(raw)
    if not drive:
        return
    root = f"{drive}\\"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetDriveTypeW.restype = ctypes.c_uint
    kernel32.GetVolumeInformationW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.c_wchar_p,
        ctypes.c_uint,
    ]
    kernel32.GetVolumeInformationW.restype = ctypes.c_int
    drive_type = kernel32.GetDriveTypeW(root)
    if drive_type != 3:  # DRIVE_FIXED
        raise PlatformCapabilityError(
            "only local fixed Windows volumes are supported for SQLite and artifacts"
        )
    filesystem = ctypes.create_unicode_buffer(32)
    if not kernel32.GetVolumeInformationW(
        root, None, 0, None, None, None, filesystem, len(filesystem)
    ):
        raise PlatformCapabilityError(f"unable to identify filesystem for {root}")
    if filesystem.value.upper() != "NTFS":
        raise PlatformCapabilityError(
            f"{filesystem.value} is not supported; only local NTFS supports SQLite WAL and atomic artifacts"
        )


def fsync_file(file_or_fd: IO[bytes] | int) -> None:
    """Flush a file, preserving the existing POSIX durability behaviour."""
    fd = file_or_fd if isinstance(file_or_fd, int) else file_or_fd.fileno()
    os.fsync(fd)


def fsync_directory(path: str | Path) -> None:
    """Flush a directory where the platform exposes that capability.

    Windows has no ordinary directory fd accepted by the CRT.  Directory
    metadata durability is therefore explicitly a capability downgrade until
    a native ``CreateFile(FILE_FLAG_BACKUP_SEMANTICS)`` adapter is installed.
    """
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def set_private_permissions(
    path: str | Path, *, directory: bool = False, sensitive: bool = False
) -> None:
    """Apply private storage permissions, failing closed for secrets.

    ``chmod`` on Windows only changes the read-only attribute.  Use ``icacls``
    when available to remove inheritance and grant the current account and
    SYSTEM access.  A missing ACL tool is tolerated for ordinary artifacts but
    never for an API-key file.
    """
    target = Path(path)
    if os.name != "nt":
        os.chmod(target, 0o700 if directory else 0o600)
        return

    username = os.environ.get("USERNAME")
    if not username:
        if sensitive:
            raise PlatformCapabilityError("Windows current user is unavailable for ACL setup")
        warnings.warn(f"private permissions could not be established for {target}", RuntimeWarning)
        return
    inheritance = "(OI)(CI)" if directory else ""
    try:
        result = subprocess.run(
            [
                "icacls",
                str(target),
                "/inheritance:r",
                "/grant:r",
                f"{username}:{inheritance}F",
                f"*S-1-5-18:{inheritance}F",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        if sensitive:
            raise PlatformCapabilityError(f"Windows ACL support is unavailable for {target}") from exc
        warnings.warn(f"private permissions could not be established for {target}", RuntimeWarning)
        return
    if result.returncode != 0:
        if sensitive:
            raise PlatformCapabilityError(
                f"Windows ACL setup failed for {target}: {result.stderr.strip()[:200]}"
            )
        warnings.warn(f"Windows ACL setup failed for {target}", RuntimeWarning)


def normalize_manifest_path(value: str) -> str:
    """Return a safe relative POSIX path for manifests and graph evidence.

    This deliberately rejects names that Windows silently aliases (ADS,
    device names, trailing dots/spaces and case-fold collisions are handled by
    :func:`manifest_collision_key`).
    """
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("path contains NUL or is not text")
    normalized = unicodedata.normalize("NFC", value.replace("\\", "/"))
    drive, tail = ntpath.splitdrive(normalized)
    if drive or normalized.startswith("/") or normalized.startswith("//"):
        raise ValueError("path must be relative")
    parts = normalized.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("path contains an unsafe component")
    for part in parts:
        if part.endswith((".", " ")) or any(char in part for char in '<>"|?*:'):
            raise ValueError("path uses a Windows-reserved namespace")
        stem = part.split(".", 1)[0].rstrip(" .").upper()
        if stem in {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"}:
            raise ValueError("path uses a DOS device name")
    return PurePosixPath(*parts).as_posix()


def manifest_collision_key(value: str) -> str:
    """Return the Windows canonical comparison key for a safe manifest path."""
    normalized = normalize_manifest_path(value)
    return "/".join(part.rstrip(" .").casefold() for part in normalized.split("/"))


def atomic_replace(source: str | Path, target: str | Path, *, retries: int = 5) -> None:
    """Replace a path, retrying only known transient Windows sharing errors."""
    source_path = Path(source)
    target_path = Path(target)
    for attempt in range(max(1, retries)):
        try:
            os.replace(source_path, target_path)
            return
        except OSError as exc:
            if not _is_transient_windows_sharing_error(exc) or attempt + 1 >= retries:
                raise
            time.sleep(0.05 * (2**attempt))


def atomic_rename(source: str | Path, target: str | Path, *, retries: int = 5) -> None:
    """Rename without replacing an existing target (CAS publication)."""
    source_path = Path(source)
    target_path = Path(target)
    for attempt in range(max(1, retries)):
        try:
            os.rename(source_path, target_path)
            return
        except OSError as exc:
            # MoveFileEx, which backs Python's Windows rename, can report
            # ERROR_ACCESS_DENIED rather than ERROR_ALREADY_EXISTS when the
            # destination is an existing directory.  CAS publication must
            # surface that case as EEXIST so the caller can verify and reuse
            # the immutable object instead of treating it as a sharing lock.
            if (
                os.name == "nt"
                and getattr(exc, "winerror", None) == 5
                and target_path.exists()
            ):
                raise FileExistsError(
                    errno.EEXIST, "destination already exists", os.fspath(target_path)
                ) from exc
            if not _is_transient_windows_sharing_error(exc) or attempt + 1 >= retries:
                raise
            time.sleep(0.05 * (2**attempt))


def remove_tree(path: str | Path, *, retries: int = 5) -> None:
    """Remove a tree with bounded retries for antivirus/indexer sharing locks."""
    target = Path(path)
    for attempt in range(max(1, retries)):
        try:
            shutil.rmtree(target)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if not _is_transient_windows_sharing_error(exc) or attempt + 1 >= retries:
                raise
            time.sleep(0.05 * (2**attempt))


def terminate_process(process_or_pid: Any, *, tree: bool = True, grace_seconds: float = 5) -> None:
    """Terminate a process and, where needed, all descendants."""
    process = process_or_pid if hasattr(process_or_pid, "pid") else None
    pid = int(process.pid if process is not None else process_or_pid)
    if process is not None and process.poll() is not None:
        return
    if os.name == "nt":
        if tree:
            _run_taskkill(pid, force=False)
        elif process is not None:
            process.terminate()
        deadline = time.monotonic() + max(0.0, grace_seconds)
        state = inspect_process(pid)
        while state == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
            state = inspect_process(pid)
        if state != "stopped":
            if tree:
                _run_taskkill(pid, force=True)
            elif process is not None:
                process.kill()
        if process is not None:
            process.wait()
        elif inspect_process(pid) != "stopped":
            raise PlatformCapabilityError(f"process tree did not stop (pid={pid})")
        return
    try:
        if tree:
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + max(0.0, grace_seconds)
    state = inspect_process(pid)
    while state == "running" and time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            break
        time.sleep(0.05)
        state = inspect_process(pid)
    if state != "stopped":
        try:
            if tree:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process is not None:
        process.wait()


def _run_taskkill(pid: int, *, force: bool) -> None:
    command = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        command.append("/F")
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=10
    )
    if result.returncode not in {0, 128} and "not found" not in result.stderr.lower():
        raise PlatformCapabilityError(result.stderr.strip()[:300] or "taskkill failed")


def process_exists(pid: int) -> bool:
    """Non-destructive process existence check."""
    return inspect_process(pid) == "running"


def inspect_process(pid: int) -> Literal["running", "stopped", "unknown"]:
    """Inspect a PID without sending it a signal or treating access denial as dead."""
    if pid <= 0:
        return "stopped"
    if os.name == "nt":
        # ``os.kill(pid, 0)`` is destructive on Windows on some Python builds.
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        # PROCESS_QUERY_LIMITED_INFORMATION is needed for GetExitCodeProcess;
        # SYNCHRONIZE makes the non-destructive existence probe reliable.
        handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            return "unknown" if error == 5 else "stopped"
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return "unknown"
            return "running" if code.value == 259 else "stopped"
        finally:
            kernel32.CloseHandle(handle)
    proc_dir = Path(f"/proc/{pid}")
    if Path("/proc").is_dir():
        if not proc_dir.exists():
            return "stopped"
        try:
            stat_line = proc_dir.joinpath("stat").read_text(encoding="ascii")
            stat_end = stat_line.rfind(")")
            stat_fields = stat_line[stat_end + 2 :].split()
            if stat_fields and stat_fields[0] == "Z":
                return "stopped"
        except OSError:
            return "unknown"
    try:
        os.kill(pid, 0)
    except PermissionError:
        return "unknown"
    except ProcessLookupError:
        return "stopped"
    except OSError:
        return "unknown"
    return "running"


def process_start_time(pid: int) -> int | None:
    """Return a stable process-start token when the OS exposes one."""
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x0400 | 0x00100000, False, pid)
        if not handle:
            return None
        try:
            created = ctypes.c_ulonglong()
            exit_time = ctypes.c_ulonglong()
            kernel_time = ctypes.c_ulonglong()
            user_time = ctypes.c_ulonglong()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return None
            return int(created.value)
        finally:
            kernel32.CloseHandle(handle)
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        stat_end = stat_line.rfind(")")
        fields = stat_line[stat_end + 2 :].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def process_image_path(pid: int) -> str | None:
    """Return the executable image for a process when the OS permits it."""
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = ctypes.c_ulong(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                return None
            return os.path.abspath(buffer.value)
        finally:
            kernel32.CloseHandle(handle)
    try:
        return str(Path(f"/proc/{pid}/exe").resolve(strict=True))
    except OSError:
        return None


def process_command_line(pid: int) -> str | None:
    """Return a process command line for identity checks when available."""
    if pid <= 0:
        return None
    if os.name == "nt":
        # QueryFullProcessImageNameW deliberately exposes only the image path.
        # PowerShell's CIM provider is available on supported Windows runners
        # and lets the lifecycle helper verify its per-instance nonce too.
        query = (
            "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = "
            f"{pid}'; "
            "if ($null -ne $p) { [Console]::Write($p.CommandLine) }"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", query],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout or None
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip() or None


class InterProcessLock:
    """A small exclusive lock usable by both the POSIX and Windows helpers."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.handle: Any = None

    def __enter__(self) -> "InterProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            import msvcrt

            self.path.touch(exist_ok=True)
            self.handle = self.path.open("r+b")
            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                self.handle.write(b"\0")
                self.handle.flush()
            self.handle.seek(0)
            try:
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
            except BaseException:
                self.handle.close()
                self.handle = None
                raise
        else:
            import fcntl

            self.handle = self.path.open("a+b")
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
            except BaseException:
                self.handle.close()
                self.handle = None
                raise
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.handle is None:
            return
        if os.name == "nt":
            import msvcrt

            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


def _is_transient_windows_sharing_error(error: OSError) -> bool:
    return os.name == "nt" and getattr(error, "winerror", None) in {5, 32, 33}

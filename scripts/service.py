#!/usr/bin/env python3
"""Cross-platform build and lifecycle helper for the CodeEvolution Web service."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import nullcontext
from pathlib import Path

from codeevolution.platform import (
    InterProcessLock,
    atomic_replace,
    inspect_process,
    process_command_line,
    process_exists,
    process_image_path,
    process_start_time,
    set_private_permissions,
    start_process,
    terminate_process,
)

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
RUN_DIR = ROOT / ".run"
PID_FILE = RUN_DIR / "codeevolution.pid"
LOG_FILE = RUN_DIR / "codeevolution.log"
LEGACY_PID_FILE = RUN_DIR / "codehistory.pid"


def run(command: list[str], cwd: Path = ROOT) -> None:
    print(f"[codeevolution] {' '.join(command)}")
    subprocess.run(command, cwd=cwd, check=True)


def build() -> None:
    npm = "npm.cmd" if os.name == "nt" else "npm"
    # Native Rollup/esbuild packages differ by OS and CPU.  Always rehydrate
    # from the lockfile instead of reusing a node_modules tree from WSL/Linux.
    run([npm, "ci"], WEB)
    run([npm, "run", "build"], WEB)


def read_pid() -> int | None:
    for path in (PID_FILE, LEGACY_PID_FILE):
        try:
            raw = path.read_text(encoding="utf-8").strip()
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = raw
            return int(value.get("pid") if isinstance(value, dict) else value)
        except (OSError, ValueError):
            continue
    return None


def read_metadata() -> dict | None:
    try:
        value = json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) and isinstance(value.get("pid"), int) else None


def _write_metadata(
    process: subprocess.Popen[bytes], host: str, port: int, nonce: str
) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    set_private_permissions(RUN_DIR, directory=True)
    temporary = PID_FILE.with_name(f".{PID_FILE.name}.{process.pid}.tmp")
    payload = {
        "pid": process.pid,
        "started": process_start_time(process.pid),
        "nonce": nonce,
        "command": server_command(host, port, nonce),
        "host": host,
        "port": port,
        "platform": os.name,
    }
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        set_private_permissions(temporary)
        atomic_replace(temporary, PID_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def clear_pid_files() -> None:
    PID_FILE.unlink(missing_ok=True)
    LEGACY_PID_FILE.unlink(missing_ok=True)


def is_running(pid: int | None = None) -> bool:
    pid = pid or read_pid()
    return bool(pid and process_exists(pid))


def _managed_process_matches(pid: int | None) -> bool:
    if not pid or not process_exists(pid):
        return False
    metadata = read_metadata()
    if not metadata or metadata.get("pid") != pid:
        return False
    expected = metadata.get("started")
    actual = process_start_time(pid)
    if expected is None or actual is None or expected != actual:
        return False
    command = metadata.get("command")
    if not isinstance(command, list) or not command or not isinstance(command[0], str):
        return False
    image = process_image_path(pid)
    if image is None:
        return False
    if os.name == "nt":
        image_matches = os.path.normcase(os.path.abspath(image)) == os.path.normcase(
            os.path.abspath(command[0])
        )
    else:
        image_matches = os.path.realpath(image) == os.path.realpath(command[0])
    if not image_matches:
        return False
    nonce = metadata.get("nonce")
    command_line = process_command_line(pid)
    return isinstance(nonce, str) and bool(nonce) and command_line is not None and nonce in command_line


def _service_endpoint(metadata: dict | None) -> tuple[str, int] | None:
    """Return the endpoint recorded for a managed service instance."""
    if not metadata:
        return None
    host = metadata.get("host")
    port = metadata.get("port")
    if not isinstance(host, str) or not host:
        return None
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    return host, port


def _wait_for_service_shutdown(
    pid: int,
    endpoint: tuple[str, int] | None,
    *,
    timeout: float = 5,
) -> tuple[bool, bool]:
    """Wait for the leader and its HTTP endpoint to disappear.

    A process can exit before a descendant releases the listening socket. The
    old implementation treated the leader's exit as sufficient and could
    immediately start a second instance while the old one still owned the
    port. Keep both conditions in the shutdown contract.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        process_stopped = not process_exists(pid)
        port_released = endpoint is None or port_is_available(*endpoint)
        if process_stopped and port_released:
            return True, True
        if time.monotonic() >= deadline:
            return process_stopped, port_released
        time.sleep(0.1)


def server_command(host: str, port: int, nonce: str | None = None) -> list[str]:
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable = str(python) if python.exists() else sys.executable
    # Run the source tree as a module. A console script in an old virtualenv may
    # still point at a previously installed CodeEvolution build.
    command = [executable, "-m", "codeevolution.cli", "web", "--host", host, "--port", str(port)]
    if nonce:
        command.extend(["--instance-nonce", nonce])
    return command


def port_is_available(host: str, port: int) -> bool:
    """Return false when another process already owns the requested address."""
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    try:
        addresses = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM, 0, socket.AI_PASSIVE)
    except OSError:
        return False
    seen: set[tuple[int, tuple]] = set()
    for family, socktype, proto, _canonname, sockaddr in addresses:
        key = (family, sockaddr)
        if key in seen:
            continue
        seen.add(key)
        with socket.socket(family, socktype, proto) as probe:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            # SO_REUSEADDR weakens the occupied-port check on Windows and is
            # unnecessary for a short-lived probe on either platform.
            try:
                probe.bind(sockaddr)
            except OSError:
                return False
    return bool(seen)


def api_is_ready(host: str, port: int) -> bool:
    connect_host = "127.0.0.1" if host == "0.0.0.0" else ("::1" if host == "::" else host)
    url_host = f"[{connect_host}]" if ":" in connect_host else connect_host
    try:
        with urllib.request.urlopen(f"http://{url_host}:{port}/api/repos", timeout=0.3) as response:
            return response.status == 200 and "application/json" in response.headers.get_content_type()
    except (OSError, urllib.error.URLError):
        return False


def _terminate_started_process(process: subprocess.Popen[bytes]) -> None:
    """Terminate a failed startup child and preserve a useful diagnostic."""
    try:
        terminate_process(process, tree=True)
    except BaseException as error:
        raise RuntimeError(
            f"failed to terminate service pid={process.pid}; inspect {LOG_FILE}: {error}"
        ) from error


def start(host: str, port: int, should_build: bool = True, *, _lock_held: bool = False) -> None:
    if not 1 <= port <= 65535:
        raise RuntimeError("port must be between 1 and 65535")
    lock = nullcontext() if _lock_held else InterProcessLock(PID_FILE.with_name(f"{PID_FILE.name}.lock"))
    with lock:
        pid = read_pid()
        process_state = inspect_process(pid or 0)
        if process_state == "unknown":
            raise RuntimeError(f"service pid={pid} state is unknown; refusing to manage it")
        if process_state == "running":
            if not _managed_process_matches(pid):
                raise RuntimeError(f"service pid={pid} identity is unknown; refusing to start another instance")
            print(f"[codeevolution] already running (pid={pid})")
            return
        clear_pid_files()
        if not port_is_available(host, port):
            raise RuntimeError(
                f"port {host}:{port} is already in use; stop the existing service "
                "or choose another port with --port"
            )
        if should_build:
            build()
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        set_private_permissions(RUN_DIR, directory=True)
        log = LOG_FILE.open("ab")
        nonce = os.urandom(16).hex()
        try:
            process = start_process(
                server_command(host, port, nonce),
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env={**os.environ, "CODEEVOLUTION_INSTANCE_NONCE": nonce},
                new_process_group=False,
                no_window=True,
            )
        except BaseException:
            log.close()
            clear_pid_files()
            raise
        log.close()
        try:
            _write_metadata(process, host, port, nonce)
        except BaseException:
            try:
                _terminate_started_process(process)
            finally:
                clear_pid_files()
            raise
        for _ in range(50):
            if process.poll() is not None:
                clear_pid_files()
                raise RuntimeError(f"service exited early; inspect {LOG_FILE}")
            if api_is_ready(host, port):
                print(f"[codeevolution] started pid={process.pid} http://{host}:{port}")
                return
            time.sleep(0.1)
        try:
            _terminate_started_process(process)
        finally:
            clear_pid_files()
        raise RuntimeError(f"service did not listen on {host}:{port}; inspect {LOG_FILE}")


def stop(*, _lock_held: bool = False) -> None:
    lock = nullcontext() if _lock_held else InterProcessLock(PID_FILE.with_name(f"{PID_FILE.name}.lock"))
    with lock:
        pid = read_pid()
        metadata = read_metadata()
        endpoint = _service_endpoint(metadata)
        process_state = inspect_process(pid or 0)
        if process_state == "unknown":
            raise RuntimeError(f"service pid={pid} state is unknown; refusing to terminate it")
        if process_state != "running":
            if endpoint is not None and not port_is_available(*endpoint):
                host, port = endpoint
                raise RuntimeError(
                    f"service pid={pid} is not running but {host}:{port} is still occupied"
                )
            clear_pid_files()
            print("[codeevolution] not running")
            return
        assert pid is not None
        expected_start = metadata.get("started") if metadata else None
        actual_start = process_start_time(pid)
        if (
            expected_start is None
            or actual_start is None
            or expected_start != actual_start
            or not _managed_process_matches(pid)
        ):
            raise RuntimeError(f"service pid={pid} identity is unknown; refusing to terminate it")
        terminate_process(pid, tree=True, grace_seconds=5)
        process_stopped, port_released = _wait_for_service_shutdown(pid, endpoint)
        if not process_stopped:
            raise RuntimeError(f"service pid={pid} did not stop within 5 seconds")
        if not port_released:
            host, port = endpoint or ("", 0)
            raise RuntimeError(
                f"service pid={pid} stopped but {host}:{port} is still occupied"
            )
        clear_pid_files()
        print(f"[codeevolution] stopped pid={pid}")


def restart(host: str, port: int, should_build: bool = True) -> None:
    """Restart while holding one lifecycle lock across stop and start."""
    with InterProcessLock(PID_FILE.with_name(f"{PID_FILE.name}.lock")):
        stop(_lock_held=True)
        start(host, port, should_build, _lock_held=True)


def status() -> None:
    pid = read_pid()
    metadata = read_metadata()
    endpoint = _service_endpoint(metadata)
    process_state = inspect_process(pid or 0)
    if process_state == "unknown":
        state = f"unknown (pid={pid}; process access was denied)"
    elif process_state != "running":
        state = "stopped"
    elif not _managed_process_matches(pid):
        state = f"unknown (pid={pid}; refusing to manage it)"
    else:
        metadata = metadata or {}
        host = str(metadata.get("host") or "127.0.0.1")
        port = int(metadata.get("port") or 8765)
        ready = api_is_ready(host, port)
        state = f"running/ready (pid={pid})" if ready else f"running/unhealthy (pid={pid})"
    if process_state != "running" and endpoint is not None and not port_is_available(*endpoint):
        state = f"{state}; port occupied ({endpoint[0]}:{endpoint[1]})"
    print(f"[codeevolution] {state}; log={LOG_FILE}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "start", "stop", "restart", "status"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-build", action="store_true", help="Skip frontend build on start")
    args = parser.parse_args()
    try:
        if args.action == "build":
            build()
        elif args.action == "start":
            start(args.host, args.port, not args.no_build)
        elif args.action == "stop":
            stop()
        elif args.action == "restart":
            restart(args.host, args.port, not args.no_build)
        else:
            status()
    except RuntimeError as error:
        raise SystemExit(f"[codeevolution] error: {error}") from None


if __name__ == "__main__":
    main()

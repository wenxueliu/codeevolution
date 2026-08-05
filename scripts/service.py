#!/usr/bin/env python3
"""Cross-platform build and lifecycle helper for the CodeHistory Web service."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
RUN_DIR = ROOT / ".run"
PID_FILE = RUN_DIR / "codehistory.pid"
LOG_FILE = RUN_DIR / "codehistory.log"


def run(command: list[str], cwd: Path = ROOT) -> None:
    print(f"[codehistory] {' '.join(command)}")
    subprocess.run(command, cwd=cwd, check=True)


def build() -> None:
    npm = "npm.cmd" if os.name == "nt" else "npm"
    if not (WEB / "node_modules").exists():
        run([npm, "ci"], WEB)
    run([npm, "run", "build"], WEB)


def read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def is_running(pid: int | None = None) -> bool:
    pid = pid or read_pid()
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def server_command(host: str, port: int) -> list[str]:
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable = str(python) if python.exists() else sys.executable
    # Run the source tree as a module. A console script in an old virtualenv may
    # still point at a previously installed CodeHistory build.
    return [executable, "-m", "codehistory.cli", "web", "--host", host, "--port", str(port)]


def port_is_available(host: str, port: int) -> bool:
    """Return false when another process already owns the requested address."""
    bind_host = host if host not in {"0.0.0.0", "::"} else "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((bind_host, port))
        except OSError:
            return False
    return True


def api_is_ready(host: str, port: int) -> bool:
    connect_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    try:
        with urllib.request.urlopen(f"http://{connect_host}:{port}/api/repos", timeout=0.3) as response:
            return response.status == 200 and "application/json" in response.headers.get_content_type()
    except (OSError, urllib.error.URLError):
        return False


def start(host: str, port: int, should_build: bool = True) -> None:
    pid = read_pid()
    if is_running(pid):
        print(f"[codehistory] already running (pid={pid})")
        return
    PID_FILE.unlink(missing_ok=True)
    if not port_is_available(host, port):
        raise RuntimeError(
            f"port {host}:{port} is already in use; stop the existing service "
            "or choose another port with --port"
        )
    if should_build:
        build()
    RUN_DIR.mkdir(exist_ok=True)
    log = LOG_FILE.open("ab")
    options = {"cwd": ROOT, "stdout": log, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(server_command(host, port), **options)
    PID_FILE.write_text(str(process.pid), encoding="utf-8")
    for _ in range(50):
        if process.poll() is not None:
            raise RuntimeError(f"service exited early; inspect {LOG_FILE}")
        if api_is_ready(host, port):
            print(f"[codehistory] started pid={process.pid} http://{host}:{port}")
            return
        time.sleep(0.1)
    if process.poll() is None:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
    PID_FILE.unlink(missing_ok=True)
    raise RuntimeError(f"service did not listen on {host}:{port}; inspect {LOG_FILE}")


def stop() -> None:
    pid = read_pid()
    if not is_running(pid):
        PID_FILE.unlink(missing_ok=True)
        print("[codehistory] not running")
        return
    assert pid is not None
    if os.name == "nt":
        os.kill(pid, signal.CTRL_BREAK_EVENT)
    else:
        os.killpg(pid, signal.SIGTERM)
    for _ in range(50):
        if not is_running(pid):
            PID_FILE.unlink(missing_ok=True)
            print(f"[codehistory] stopped pid={pid}")
            return
        time.sleep(0.1)
    raise RuntimeError(f"service pid={pid} did not stop within 5 seconds")


def status() -> None:
    pid = read_pid()
    state = f"running (pid={pid})" if is_running(pid) else "stopped"
    print(f"[codehistory] {state}; log={LOG_FILE}")


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
            stop()
            start(args.host, args.port, not args.no_build)
        else:
            status()
    except RuntimeError as error:
        raise SystemExit(f"[codehistory] error: {error}") from None


if __name__ == "__main__":
    main()

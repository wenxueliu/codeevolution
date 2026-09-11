#!/usr/bin/env python3
"""Install a built wheel in a clean venv and smoke-test its Web entrypoint."""

from __future__ import annotations

import glob
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def _python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def main() -> None:
    wheel_candidates = sorted(glob.glob(str(Path(sys.argv[1] if len(sys.argv) > 1 else "dist") / "*.whl")))
    if len(wheel_candidates) != 1:
        raise SystemExit(f"expected one wheel, found {wheel_candidates}")
    wheel = Path(wheel_candidates[0]).resolve()
    with tempfile.TemporaryDirectory(prefix="codeevolution-wheel-smoke-") as temporary:
        root = Path(temporary)
        venv = root / "venv"
        data_dir = root / "data"
        subprocess.check_call([sys.executable, "-m", "venv", str(venv)])
        target_python = _python(venv)
        subprocess.check_call([str(target_python), "-m", "pip", "install", str(wheel)])
        subprocess.check_call([str(target_python), "-m", "codeevolution.cli", "--help"], cwd=root)
        subprocess.check_call(
            [
                str(target_python),
                "-c",
                (
                    "import pathlib, codeevolution.api; "
                    "assert (pathlib.Path(codeevolution.api.__file__).parent / "
                    "'web_dist' / 'index.html').is_file()"
                ),
            ],
            cwd=root,
        )

        port = _free_port()
        environment = {**os.environ, "CODEEVOLUTION_DATA_DIR": str(data_dir)}
        process = subprocess.Popen(
            [
                str(target_python),
                "-m",
                "codeevolution.cli",
                "web",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    output = process.stdout.read().decode("utf-8", errors="replace") if process.stdout else ""
                    raise RuntimeError(f"wheel Web process exited early: {output[-1000:]}")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.5) as response:
                        if response.status == 200 and b"CodeEvolution" in response.read():
                            return
                except OSError:
                    pass
                time.sleep(0.2)
            raise RuntimeError("wheel Web process did not become ready")
        finally:
            _stop(process)


if __name__ == "__main__":
    main()

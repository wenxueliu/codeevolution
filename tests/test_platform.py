import os
import shutil
import subprocess
import sys
from pathlib import PosixPath, PurePosixPath

import pytest

from codeevolution.platform import (
    atomic_rename,
    manifest_collision_key,
    normalize_manifest_path,
    open_beneath,
    process_exists,
    set_private_permissions,
    sqlite_readonly_uri,
    terminate_process,
)


def test_sqlite_uri_escapes_windows_sensitive_filename_characters(tmp_path):
    path = tmp_path / "仓库 % # ?.db"
    uri = sqlite_readonly_uri(path)

    assert uri.endswith("%E4%BB%93%E5%BA%93%20%25%20%23%20%3F.db?mode=ro")


def test_manifest_paths_reject_windows_namespace_aliases():
    assert normalize_manifest_path("src\\main.py") == "src/main.py"
    assert manifest_collision_key("Src/main.py") == manifest_collision_key("src\\main.py")
    for value in (
        "C:/repo/file.py",
        r"\\server\share\file.py",
        "secret.txt:stream",
        "CON.txt",
        "src/name. ",
        "src/<generated>.py",
    ):
        with pytest.raises(ValueError):
            normalize_manifest_path(value)


def test_open_beneath_rejects_unsafe_relative_paths(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("pass\n")

    with pytest.raises(OSError, match="unsafe"):
        open_beneath(root, PurePosixPath("../app.py"))


def test_process_probe_is_non_destructive():
    assert process_exists(os.getpid())
    assert not process_exists(-1)


def test_windows_rename_reports_existing_directory_as_eexist(monkeypatch, tmp_path):
    import codeevolution.platform as platform

    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()

    def access_denied(_source, _target):
        error = PermissionError(13, "access denied", str(target))
        error.winerror = 5
        raise error

    monkeypatch.setattr(platform.os, "name", "nt")
    monkeypatch.setattr(platform.os, "rename", access_denied)
    monkeypatch.setattr(platform, "Path", PosixPath)

    with pytest.raises(FileExistsError) as raised:
        atomic_rename(source, target)

    assert raised.value.errno == 17


def test_pid_only_termination_escalates_without_a_popen_handle():
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
        ],
        start_new_session=(os.name != "nt"),
    )
    try:
        terminate_process(process.pid, tree=True, grace_seconds=0.05)
        assert process.wait(timeout=2) is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL regression")
def test_windows_private_acl_excludes_public_principals(tmp_path):
    if shutil.which("icacls") is None:
        pytest.fail("icacls is required on Windows")
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    set_private_permissions(private_dir, directory=True, sensitive=True)
    paths = [
        private_dir / "llm-config.json",
        private_dir / "catalog.db",
        private_dir / "catalog.db-wal",
        private_dir / "catalog.db-shm",
        private_dir / "migration.backup",
        private_dir / "artifact.bin",
    ]
    for path in paths:
        path.write_bytes(b"private")
        set_private_permissions(path, sensitive=path.name == "llm-config.json")

    for path in paths:
        result = subprocess.run(
            ["icacls", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        acl = result.stdout.lower()
        assert "everyone" not in acl
        assert "builtin\\users" not in acl

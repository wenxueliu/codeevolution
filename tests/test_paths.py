from pathlib import Path

from codeevolution.paths import (
    analysis_data_dir,
    data_dir,
    environment_value,
    repo_data_file,
    shared_data_file,
)


def test_new_environment_name_takes_precedence(monkeypatch):
    monkeypatch.setenv("CODEEVOLUTION_DATA_DIR", "/new")
    monkeypatch.setenv("CODEHISTORY_DATA_DIR", "/legacy")

    assert environment_value("CODEEVOLUTION_DATA_DIR") == "/new"


def test_data_dir_falls_back_to_existing_legacy_directory(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".codehistory").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("CODEEVOLUTION_DATA_DIR", raising=False)
    monkeypatch.delenv("CODEHISTORY_DATA_DIR", raising=False)

    assert data_dir() == home / ".codehistory"


def test_data_dir_falls_back_when_default_home_storage_is_not_writable(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    # A file at the preferred directory path makes mkdir fail without relying
    # on chmod/root behavior in the test environment.
    (home / ".codeevolution").write_text("read-only placeholder")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("CODEEVOLUTION_DATA_DIR", raising=False)
    monkeypatch.delenv("CODEHISTORY_DATA_DIR", raising=False)

    expected = Path(__file__).resolve().parents[1] / "data" / ".codeevolution"
    assert data_dir() == expected


def test_repo_data_file_prefers_new_then_existing_legacy(tmp_path):
    legacy = tmp_path / ".codehistory" / "evolution.db"
    legacy.parent.mkdir()
    legacy.touch()
    assert repo_data_file(tmp_path, "evolution.db") == legacy

    current = tmp_path / ".codeevolution" / "evolution.db"
    current.parent.mkdir()
    current.touch()
    assert repo_data_file(tmp_path, "evolution.db") == current


def test_analysis_data_dir_never_falls_back_to_legacy(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".codehistory").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("CODEEVOLUTION_DATA_DIR", raising=False)
    monkeypatch.setenv("CODEHISTORY_DATA_DIR", str(tmp_path / "legacy-setting"))

    assert analysis_data_dir() == home / ".codeevolution"

    configured = tmp_path / "snapshots"
    monkeypatch.setenv("CODEEVOLUTION_DATA_DIR", str(configured))
    assert analysis_data_dir() == configured


def test_shared_data_file_falls_back_by_file_not_directory(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".codeevolution").mkdir(parents=True)
    legacy = home / ".codehistory" / "registry.json"
    legacy.parent.mkdir()
    legacy.write_text("[]")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("CODEEVOLUTION_DATA_DIR", raising=False)
    monkeypatch.delenv("CODEHISTORY_DATA_DIR", raising=False)

    assert shared_data_file("registry.json") == legacy
    current = home / ".codeevolution" / "registry.json"
    current.write_text("[]")
    assert shared_data_file("registry.json") == current

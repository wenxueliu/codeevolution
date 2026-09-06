from pathlib import Path

from codeevolution.paths import data_dir, environment_value, repo_data_file


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


def test_repo_data_file_prefers_new_then_existing_legacy(tmp_path):
    legacy = tmp_path / ".codehistory" / "evolution.db"
    legacy.parent.mkdir()
    legacy.touch()
    assert repo_data_file(tmp_path, "evolution.db") == legacy

    current = tmp_path / ".codeevolution" / "evolution.db"
    current.parent.mkdir()
    current.touch()
    assert repo_data_file(tmp_path, "evolution.db") == current

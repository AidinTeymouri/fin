"""Tests for project root resolution, DB path and .env loading."""

import os
from pathlib import Path

from app import config
from app.config import PROJECT_ROOT, load_env
from app.database import resolve_db_path


def test_project_root_contains_backend():
    assert (PROJECT_ROOT / "backend" / "app" / "config.py").is_file()


def test_default_db_path_is_project_root_db(monkeypatch, tmp_path):
    monkeypatch.delenv("DB_PATH", raising=False)
    expected = str(PROJECT_ROOT / "db" / "finally.db")
    assert resolve_db_path() == expected


def test_default_db_path_independent_of_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    path = resolve_db_path()
    assert Path(path).is_absolute()
    assert path == str(PROJECT_ROOT / "db" / "finally.db")


def test_db_path_env_override(monkeypatch, tmp_path):
    custom = str(tmp_path / "custom.db")
    monkeypatch.setenv("DB_PATH", custom)
    assert resolve_db_path() == custom


def test_load_env_reads_project_root_env(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("FINALLY_TEST_A=from_file\nFINALLY_TEST_B=from_file\n")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("FINALLY_TEST_A", raising=False)
    monkeypatch.setenv("FINALLY_TEST_B", "from_env")
    try:
        load_env()
        assert os.environ["FINALLY_TEST_A"] == "from_file"
        # Real env vars are not overridden
        assert os.environ["FINALLY_TEST_B"] == "from_env"
    finally:
        os.environ.pop("FINALLY_TEST_A", None)


def test_load_env_missing_file_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    load_env()  # no .env present; must not raise

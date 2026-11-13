from pathlib import Path

from claude_instances import paths


def test_config_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_INSTANCES_CONFIG_DIR", str(tmp_path))
    assert paths.config_dir() == tmp_path


def test_default_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_LAUNCHER_DEFAULT_DATA_DIR", str(tmp_path))
    assert paths.default_data_dir() == tmp_path


def test_defaults(monkeypatch):
    monkeypatch.delenv("CLAUDE_INSTANCES_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_LAUNCHER_DEFAULT_DATA_DIR", raising=False)
    assert paths.config_dir() == Path.home() / ".config/claude-instances"
    assert paths.default_data_dir() == Path.home() / "Library/Application Support/Claude"
    assert paths.apps_dirs() == [Path("/Applications"), Path.home() / "Applications"]

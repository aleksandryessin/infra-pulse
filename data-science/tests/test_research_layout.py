"""The local MLflow launcher stays portable and local-only."""

import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
launcher = runpy.run_path(ROOT / "data-science/mlflow/server.py")


def test_mlflow_store_is_anchored_to_config_not_current_directory(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[server]\nhost="127.0.0.1"\nport=5059\n[storage]\ndirectory="runs"\n')
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    command, store = launcher["server_command"](config)
    assert store == tmp_path / "runs"
    assert command[command.index("--backend-store-uri") + 1] == (
        "sqlite:///" + (store / "mlflow.db").as_posix()
    )
    assert command[command.index("--artifacts-destination") + 1] == str(store / "artifacts")
    assert command[command.index("--port") + 1] == "5059"
    assert not store.exists()  # Inspecting configuration does not create a DB.


def test_local_mlflow_launcher_rejects_public_bind(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[server]\nhost="0.0.0.0"\nport=5000\n[storage]\ndirectory="runs"\n')
    with pytest.raises(ValueError, match="local-only"):
        launcher["server_command"](config)

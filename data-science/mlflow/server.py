"""Start local MLflow; SQLite and run artifacts are separate from research Parquet."""

import argparse
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).with_name("config.toml")


def server_command(config_path=DEFAULT_CONFIG, port=None, store_dir=None):
    config_path = Path(config_path).resolve()
    with config_path.open("rb") as stream:
        config = tomllib.load(stream)
    host = config["server"]["host"]
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "This launcher is local-only; configure protected shared tracking separately"
        )
    port = config["server"]["port"] if port is None else port
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Port must be in 1..65535")
    store = (
        Path(store_dir).expanduser().resolve()
        if store_dir is not None
        else (config_path.parent / config["storage"]["directory"]).resolve()
    )
    database = store / "mlflow.db"
    command = [
        sys.executable,
        "-m",
        "mlflow",
        "server",
        "--host",
        host,
        "--port",
        str(port),
        "--backend-store-uri",
        "sqlite:///" + database.as_posix(),
        "--artifacts-destination",
        str(store / "artifacts"),
    ]
    return command, store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--port", type=int)
    parser.add_argument("--store-dir", type=Path)
    parser.add_argument("--print-command", action="store_true")
    args = parser.parse_args()
    command, store = server_command(args.config, args.port, args.store_dir)
    if args.print_command:
        print(shlex.join(command))
        return
    (store / "artifacts").mkdir(parents=True, exist_ok=True)
    try:
        raise SystemExit(subprocess.call(command))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()

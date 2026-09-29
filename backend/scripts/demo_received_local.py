"""Run the synthetic received-to-UI path on disposable local PostgreSQL.

This is a development demo, not customer ingestion or an authenticated service.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "backend/fixtures/received-batch.synthetic.json"
FOLLOWUP_FIXTURE = ROOT / "backend/fixtures/received-batch-followup.synthetic.json"


def executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"{name} is required on PATH")
    return path


def free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_for(url: str, process: subprocess.Popen, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"local service exited before becoming ready: {url}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    raise RuntimeError(f"local service did not become ready: {url}")


def wait_for_received_rows(url: str, watcher: subprocess.Popen, expected: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if watcher.poll() is not None:
            raise RuntimeError("local inbox watcher exited before importing the first batch")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if json.load(response).get("received_rows") == expected:
                    return
        except (OSError, urllib.error.URLError, ValueError):
            pass
        time.sleep(0.2)
    raise RuntimeError("first synthetic batch was not imported by the local watcher")


def stop(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def request_stop(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-port", type=int, default=8001)
    parser.add_argument("--web-port", type=int, default=5173)
    args = parser.parse_args()
    if not all(1024 <= port <= 65535 for port in (args.api_port, args.web_port)):
        parser.error("API and web ports must be 1024..65535")
    if args.api_port == args.web_port:
        parser.error("API and web ports must differ")
    if not FIXTURE.is_file() or not FOLLOWUP_FIXTURE.is_file():
        parser.error("synthetic received fixtures are missing")
    if not (ROOT / "frontend/node_modules").is_dir():
        parser.error("frontend/node_modules is missing; run npm --prefix frontend ci")
    initdb = executable("initdb")
    pg_ctl = executable("pg_ctl")
    uv = executable("uv")
    npm = executable("npm")

    db_port = free_loopback_port()
    stream = f"synthetic-demo-{uuid4().hex[:12]}"
    api: subprocess.Popen | None = None
    web: subprocess.Popen | None = None
    watcher: subprocess.Popen | None = None
    db_stopped = False
    temporary = tempfile.mkdtemp(prefix="infra-pulse-synthetic-demo-")
    previous_term = signal.signal(signal.SIGTERM, request_stop)
    try:
        pg_data = Path(temporary) / "pgdata"
        pg_log = Path(temporary) / "postgres.log"
        inbox = Path(temporary) / "inbox"
        dsn = f"postgresql://postgres@127.0.0.1:{db_port}/postgres"
        try:
            subprocess.run(
                [initdb, "-D", str(pg_data), "-A", "trust", "-U", "postgres"],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            subprocess.run(
                [
                    pg_ctl,
                    "-D",
                    str(pg_data),
                    "-l",
                    str(pg_log),
                    "-o",
                    f"-p {db_port} -h 127.0.0.1",
                    "-w",
                    "start",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            inbox.mkdir()
            watcher = subprocess.Popen(
                [
                    uv,
                    "run",
                    "--locked",
                    "python",
                    str(ROOT / "backend/scripts/load_received_batch.py"),
                    "--directory",
                    str(inbox),
                    "--stream",
                    stream,
                    "--watch",
                    "--poll-seconds",
                    "1",
                ],
                cwd=ROOT,
                env={**os.environ, "INFRA_RECEIVED_DSN": dsn},
                start_new_session=True,
            )
            api_env = {
                **os.environ,
                "INFRA_MODE": "received",
                "INFRA_DB_DSN": dsn,
                "INFRA_RECEIVED_STREAM_ID": stream,
                "INFRA_ENABLE_LOCAL_REVIEWS": "true",
            }
            api = subprocess.Popen(
                [
                    uv,
                    "run",
                    "--locked",
                    "uvicorn",
                    "infra_pulse_backend.api.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(args.api_port),
                    "--no-access-log",
                ],
                cwd=ROOT,
                env=api_env,
                start_new_session=True,
            )
            wait_for(f"http://127.0.0.1:{args.api_port}/health/ready", api)
            shutil.copyfile(FIXTURE, inbox / "initial.part")
            os.replace(inbox / "initial.part", inbox / "initial.json")
            wait_for_received_rows(
                f"http://127.0.0.1:{args.api_port}/api/v1/capabilities", watcher, 3
            )
            web = subprocess.Popen(
                [
                    npm,
                    "--prefix",
                    "frontend",
                    "run",
                    "dev",
                    "--",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(args.web_port),
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "VITE_DATA_MODE": "received",
                    "API_PROXY_TARGET": f"http://127.0.0.1:{args.api_port}",
                },
                start_new_session=True,
            )
            wait_for(f"http://127.0.0.1:{args.web_port}/queue", web)
            print(f"Synthetic local demo: http://127.0.0.1:{args.web_port}/queue", flush=True)
            print(f"Local inbox: {inbox}", flush=True)
            print(
                "Inspect a candidate and its object; save a result and reload the journal.",
                flush=True,
            )
            print(
                "To add another synthetic batch: "
                f"cp {FOLLOWUP_FIXTURE} {inbox / 'followup.part'} && "
                f"mv {inbox / 'followup.part'} {inbox / 'followup.json'}",
                flush=True,
            )
            print("Press Ctrl+C to stop and delete the temporary database.", flush=True)
            while api.poll() is None and web.poll() is None and watcher.poll() is None:
                time.sleep(0.5)
            raise RuntimeError("local API, frontend or inbox watcher exited unexpectedly")
        except KeyboardInterrupt:
            print("Stopping synthetic local demo.", flush=True)
        finally:
            previous_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            try:
                stop(web)
                stop(api)
                stop(watcher)
                if pg_data.is_dir():
                    running = (
                        subprocess.run(
                            [pg_ctl, "-D", str(pg_data), "status"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                        ).returncode
                        == 0
                    )
                    if running:
                        subprocess.run(
                            [pg_ctl, "-D", str(pg_data), "-m", "fast", "-w", "stop"],
                            check=True,
                            stdout=subprocess.DEVNULL,
                            start_new_session=True,
                        )
                db_stopped = True
            finally:
                signal.signal(signal.SIGINT, previous_int)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        if db_stopped:
            shutil.rmtree(temporary)
        else:
            print(f"Temporary database kept for manual cleanup: {temporary}", file=sys.stderr)


if __name__ == "__main__":
    main()

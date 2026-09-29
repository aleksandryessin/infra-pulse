"""Verify synthetic received records and reviews survive a PostgreSQL restart.

Creates and deletes its own loopback-only PostgreSQL cluster. This verifies local
durability, not backup/restore, customer ingress, or dispatcher authorization.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient
from load_received_batch import load

from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "backend/fixtures/received-batch.synthetic.json"


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)


def main() -> None:
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if initdb is None or pg_ctl is None:
        raise RuntimeError("initdb and pg_ctl are required on PATH")
    if not FIXTURE.is_file():
        raise RuntimeError("synthetic received fixture is missing")

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    with tempfile.TemporaryDirectory(prefix="infra-pulse-review-restart-") as temporary:
        data = Path(temporary) / "pgdata"
        log = Path(temporary) / "postgres.log"
        run([initdb, "-D", str(data), "-A", "trust", "-U", "postgres"])
        base = [pg_ctl, "-D", str(data)]
        start = base + ["-l", str(log), "-o", f"-p {port} -h 127.0.0.1", "-w", "start"]
        stop = base + ["-m", "fast", "-w", "stop"]
        run(start)
        running = True
        try:
            dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"
            stream = f"synthetic-restart-{uuid4().hex}"
            assert load(FIXTURE, dsn=dsn, stream_id=stream)["rows"] == 3
            settings = Settings(
                mode="received",
                db_dsn=dsn,
                received_stream_id=stream,
                enable_local_reviews=True,
                _env_file=None,
            )
            before = TestClient(create_app(settings))
            queue = before.get("/api/v1/attention").json()
            row_uid = next(
                item["message"]["row_uid"] for item in queue["items"] if item["message"]["alarm"]
            )
            payload = {
                "idempotency_key": str(uuid4()),
                "expected_revision": 0,
                "view_as_of": queue["as_of"],
                "displayed_received_watermark": queue["received_watermark"],
                "displayed_snapshot_id": stream,
                "displayed_policy_version": queue["policy_version"],
                "action_text": "Синтетическая проверка",
                "result_text": "Ничего не обнаружено",
                "reason_text": "Проверка сохранности после перезапуска",
            }
            saved = before.post(f"/api/v1/attention/{row_uid}/reviews", json=payload)
            assert saved.status_code == 201, saved.text

            run(stop)
            running = False
            assert before.get("/health/ready").status_code == 503
            assert before.get("/api/v1/attention").status_code == 503
            run(start)
            running = True

            after = TestClient(create_app(settings))
            assert after.get("/health/ready").status_code == 200
            current = after.get("/api/v1/attention").json()
            assert current["all_records_total"] == 3
            source = next(
                item for item in current["items"] if item["message"]["row_uid"] == row_uid
            )
            assert source["message"]["alarm"] is True
            assert source["local_review_revision"] == 1
            history = after.get(f"/api/v1/attention/{row_uid}/reviews").json()
            journal = after.get("/api/v1/review-journal", params={"row_uid": row_uid}).json()
            assert history["revision"] == 1 and len(history["items"]) == 1
            assert journal["total"] == 1
            assert journal["items"][0]["message"]["row_uid"] == row_uid
            assert journal["items"][0]["note"]["result_text"] == "Ничего не обнаружено"
            with psycopg.connect(dsn) as connection:
                counts = connection.execute(
                    """SELECT
                        (SELECT count(*) FROM dispatch_observations
                         WHERE namespace_id = %s AND snapshot_id = %s),
                        (SELECT count(*) FROM replay_review_notes
                         WHERE namespace_id = %s AND snapshot_id = %s),
                        (SELECT count(*) FROM replay_review_audit
                         WHERE namespace_id = %s AND snapshot_id = %s)""",
                    ("local-received", stream) * 3,
                ).fetchone()
            assert counts == (3, 1, 1), counts
            print(
                json.dumps(
                    {
                        "check": "synthetic_postgresql_restart",
                        "result": "passed",
                        "source_rows": counts[0],
                        "review_notes": counts[1],
                        "audit_rows": counts[2],
                        "outage_readiness": 503,
                        "outage_queue": 503,
                        "recovered_readiness": 200,
                    }
                )
            )
        finally:
            if running:
                run(stop)


if __name__ == "__main__":
    main()

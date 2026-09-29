"""Job loop of the ingestion worker.

    INFRA_DB_DSN=... INFRA_RECEIVED_STREAM_ID=... python -m infra_pulse_backend.worker

Applies the idempotent SQL migrations when its role may create objects in the schema
(the owner; on the stand the ``migrate`` service applies them and the worker connects
as the runtime role ``infra_pulse_app``), then processes queued imports in upload
order. It wakes on ``NOTIFY infra_import_jobs`` from the API or every
``INFRA_WORKER_POLL_SECONDS``. SIGTERM/SIGINT stop it after the current job; a job
interrupted harder is claimed again once its lease expires.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from uuid import uuid4

import psycopg

from infra_pulse_backend.config import Settings
from infra_pulse_backend.ingestion.imports_pg import NOTIFY_CHANNEL
from infra_pulse_backend.ingestion.load_pg import lock_received_scope
from infra_pulse_backend.storage.migrations_pg import apply_migrations, can_apply_migrations
from infra_pulse_backend.worker.runner import WorkerConfig, resolve_recompute, run_once

log = logging.getLogger("infra_pulse.worker")


def migrate_at_start(dsn: str, directory: Path) -> int | None:
    """Apply the migrations as the owner; None when the role may not create (runtime role)."""
    allowed, user, schema = can_apply_migrations(dsn)
    if not allowed:
        log.info(
            "role %s may not create in schema %s: migrations are applied by the migrate service",
            user,
            schema,
        )
        return None
    return apply_migrations(dsn, directory)


def config_from(settings: Settings) -> WorkerConfig:
    if settings.db_dsn is None:
        raise SystemExit("INFRA_DB_DSN is required")
    if not settings.received_stream_id:
        raise SystemExit("INFRA_RECEIVED_STREAM_ID is required: the worker writes there")
    if not 0.2 <= settings.worker_poll_seconds <= 3600:
        raise SystemExit("INFRA_WORKER_POLL_SECONDS must be 0.2–3600")
    return WorkerConfig(
        dsn=settings.db_dsn.get_secret_value(),
        upload_dir=settings.upload_dir,
        namespace_id=settings.received_namespace,
        stream_id=settings.received_stream_id,
        worker_id=f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}",
        lease_seconds=settings.worker_lease_seconds,
        retry_seconds=settings.worker_retry_seconds,
    )


class _Wakeup:
    """LISTEN connection; reopened after a storage failure."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.connection: psycopg.Connection | None = None

    def wait(self, timeout: float) -> None:
        if self.connection is None or self.connection.closed:
            self.connection = psycopg.connect(self.dsn, autocommit=True)
            self.connection.execute(f"LISTEN {NOTIFY_CHANNEL}")
        for _ in self.connection.notifies(timeout=timeout, stop_after=1):
            pass

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m infra_pulse_backend.worker")
    parser.add_argument("--once", action="store_true", help="process pending jobs and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    settings = Settings()
    config = config_from(settings)
    if settings.worker_migrations_dir is not None:
        count = migrate_at_start(config.dsn, settings.worker_migrations_dir)
        if count is not None:
            log.info("applied %d migrations", count)
    config.upload_dir.mkdir(parents=True, exist_ok=True)
    # An empty received scope makes /health/ready meaningful before the first upload.
    with psycopg.connect(config.dsn) as connection:
        lock_received_scope(
            connection, namespace_id=config.namespace_id, stream_id=config.stream_id
        )
    recompute = resolve_recompute()
    log.info(
        "worker %s: scope %s/%s, recompute %s",
        config.worker_id,
        config.namespace_id,
        config.stream_id,
        getattr(recompute, "__module__", "?"),
    )

    stop = threading.Event()
    previous = {
        signum: signal.signal(signum, lambda *_: stop.set())
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    wakeup = _Wakeup(config.dsn)
    try:
        while not stop.is_set():
            try:
                while not stop.is_set() and run_once(config, recompute):
                    pass
                if args.once:
                    return 0
                wakeup.wait(settings.worker_poll_seconds)
            except psycopg.OperationalError:
                log.exception("storage unavailable; retrying")
                wakeup.close()
                stop.wait(settings.worker_poll_seconds)
    finally:
        wakeup.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

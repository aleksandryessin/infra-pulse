"""Ingestion worker (package B1): ``python -m infra_pulse_backend.worker``.

A separate process from the same backend image. HTTP stores an upload and queues a
job; the worker parses the CSV, loads PostgreSQL, calls the forecast recompute and
sets the import status. Never imported by the HTTP application.
"""

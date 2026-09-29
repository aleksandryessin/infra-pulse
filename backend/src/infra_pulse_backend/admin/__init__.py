"""Administrator command line (B1x): ``python -m infra_pulse_backend.admin token ...``.

Runs on the server next to the API with the same ``INFRA_DB_DSN``; every action is
written to ``audit_events``. Imports only storage code: no research, MLflow or HTTP.
"""

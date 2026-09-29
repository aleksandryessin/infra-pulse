from pathlib import Path
from typing import Literal

from pydantic import PositiveFloat, PositiveInt, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INFRA_", env_file=".env", extra="ignore")

    # No live inference mode is implemented yet. An explicit fixture mode unblocks the UI.
    mode: Literal["scaffold", "fixture", "replay", "received"] = "scaffold"
    db_dsn: SecretStr | None = None
    replay_namespace: str = "local-replay"
    replay_snapshot_id: str | None = None
    received_namespace: str = "local-received"
    received_stream_id: str | None = None
    enable_local_reviews: bool = False
    # Reserved runtime paths; scaffold/fixture do not read them yet.
    # Research data and outputs live separately under data-science/.
    data_root: Path = Path("var/data")
    artifact_root: Path = Path("var/models")
    # C0 (27.09): settings shared by the ingestion worker (B1), auth (B3) and research page.
    # Uploaded files are stored here by HTTP and processed by the worker (never in Git).
    upload_dir: Path = Path("var/uploads")
    # dev_stub: no login, every caller is the local operator (local or tunnel-only stand);
    # ldap: directory login and server-side role checks (B3). Public stands require ldap.
    auth_mode: Literal["dev_stub", "ldap"] = "dev_stub"
    # Aggregated research summary served by /api/v1/research-summary outside fixture mode.
    research_summary_path: Path | None = None
    # B1 (27.09): ingestion worker `python -m infra_pulse_backend.worker`. It loads journal
    # CSV into the received scope (received_namespace / received_stream_id), so both are
    # required by the worker. Jobs wait for LISTEN/NOTIFY or poll every worker_poll_seconds;
    # a claimed job's lease is renewed at every stage and reclaimed after expiry.
    worker_poll_seconds: float = 5.0
    worker_lease_seconds: float = 300.0
    worker_retry_seconds: float = 10.0
    # Idempotent SQL migrations applied by the worker at start; unset to skip.
    worker_migrations_dir: Path | None = Path("backend/migrations")
    # B3 (27.09): directory login for auth_mode=ldap. Values come from the server .env.
    # ldap://lldap:3890 on the stand (Compose network only); ldaps:// verifies the server.
    ldap_url: str | None = None
    # lldap layout: users are uid=<name>,ou=people,<base_dn>; groups cn=<role>,ou=groups.
    ldap_base_dn: str | None = None
    ldap_timeout_seconds: PositiveFloat = 5.0
    # Absolute session lifetime: the server row and both cookies expire together.
    session_ttl_minutes: PositiveInt = 480
    # Login throttle: failed attempts per username within the window (per address x4).
    login_max_failures: PositiveInt = 5
    login_window_seconds: PositiveInt = 300
    # Serve Swagger UI /api/docs and the schema /api/openapi.json when auth_mode=ldap
    # (dev_stub always does). Only the documentation opens; every route keeps its check.
    public_docs: bool = False
    # B1x (27.09): observation API POST /api/v1/observations for external systems with
    # `Authorization: Bearer ipk_...` (tokens: `python -m infra_pulse_backend.admin token`).
    # Batches accepted per token per minute; above it 429 (in-process window, like login).
    integration_requests_per_minute: PositiveInt = 60
    # G1 (27.09): request audit (api/audit_log.py, ТЗ §11), written when INFRA_DB_DSN is
    # set. Polls are summed per actor and route over the window; the same view repeated
    # within the fold interval is counted instead of written again.
    audit_poll_window_seconds: PositiveFloat = 900.0
    audit_fold_seconds: PositiveFloat = 300.0

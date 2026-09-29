"""Apply the operational PostgreSQL migrations, then enable the runtime role.

Runs as the schema owner (``INFRA_DB_DSN``, POSTGRES_USER) before the API and the
worker start: the ``migrate`` service of deploy/compose.server.yaml. With
``INFRA_DB_APP_PASSWORD`` it also sets LOGIN and the password of ``infra_pulse_app``
and checks one login (``infra_pulse_backend.storage.migrations_pg``). Exit code 1
when the role is missing or not least-privilege.
"""

from pathlib import Path

from infra_pulse_backend.storage.migrations_pg import main

MIGRATION_DIR = Path(__file__).resolve().parents[1] / "migrations"


if __name__ == "__main__":
    raise SystemExit(main(MIGRATION_DIR))

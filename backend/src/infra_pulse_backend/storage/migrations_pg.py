"""Operational migrations and the runtime PostgreSQL role (SEC-03, SEC-04).

The schema owner (``POSTGRES_USER``) applies every file of ``backend/migrations`` in
number order, in one transaction and under ``MIGRATION_LOCK``. The last file,
``9000_runtime_role.sql``, creates the runtime role ``infra_pulse_app`` (NOLOGIN) and
grants it only the DML the API and the worker need.

After the migrations the deploy (``migrate`` service, ``backend/scripts/
migrate_operational_db.py``) checks the role (no superuser or other elevated
attribute, no membership, no owned object, no CREATE on the database or the schema),
turns LOGIN on with the password of ``INFRA_DB_APP_PASSWORD`` and logs in with it once.
The SCRAM verifier is computed here by libpq, so the plain password never reaches the
server, its log or a command line. Any problem stops the deploy before the API and the
worker are recreated; a privileged role never gets LOGIN from here.

    INFRA_DB_DSN=<owner DSN> [INFRA_DB_APP_PASSWORD=...] \\
      python -m infra_pulse_backend.storage.migrations_pg

The worker applies migrations at start only when its role may create objects in the
schema, i.e. when it still connects as the owner (rollback to the owner DSN).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

RUNTIME_ROLE = "infra_pulse_app"
# pg_advisory_xact_lock key: the migrate service and a worker never migrate at once.
MIGRATION_LOCK = 0x1B1_0012
# URL-unreserved characters only: the password is embedded in the DSN of api and worker.
PASSWORD_PATTERN = re.compile(r"[A-Za-z0-9._~-]{24,128}")
DEFAULT_MIGRATIONS_DIR = Path("backend/migrations")


def migration_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def apply_migrations(dsn: str, directory: Path) -> int:
    """Apply every migration file in one transaction; returns the number of files."""
    migrations = migration_files(directory)
    if not migrations:
        raise FileNotFoundError(f"no migrations in {directory}")
    with psycopg.connect(dsn) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
        for migration in migrations:
            connection.execute(migration.read_text(encoding="utf-8"))
    return len(migrations)


def can_apply_migrations(dsn: str) -> tuple[bool, str, str | None]:
    """(may create in the current schema, current user, current schema)."""
    with psycopg.connect(dsn, autocommit=True) as connection:
        user, schema, allowed = connection.execute(
            """SELECT current_user, current_schema(),
                      coalesce(has_schema_privilege(current_schema(), 'CREATE'), false)"""
        ).fetchone()
    return bool(allowed), user, schema


def enable_runtime_login(connection: psycopg.Connection, password: str) -> None:
    """LOGIN with ``password`` for the runtime role; only a SCRAM verifier is sent."""
    if not PASSWORD_PATTERN.fullmatch(password):
        raise ValueError(
            "INFRA_DB_APP_PASSWORD must be 24-128 URL-safe characters (openssl rand -hex 32)"
        )
    if connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (RUNTIME_ROLE,)).fetchone():
        verifier = connection.pgconn.encrypt_password(
            password.encode(), RUNTIME_ROLE.encode(), b"scram-sha-256"
        ).decode("ascii")
        connection.execute(
            sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(RUNTIME_ROLE), sql.Literal(verifier)
            )
        )
        return
    raise RuntimeError(
        f"role {RUNTIME_ROLE} is missing: the migration user needs CREATEROLE, "
        "or a DBA creates it (deploy/README.md «Роли PostgreSQL»)"
    )


def runtime_role_problems(connection: psycopg.Connection) -> list[str]:
    """Reasons why the runtime role is not least-privilege in this database (empty = OK)."""
    row = connection.execute(
        """SELECT oid, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
           FROM pg_roles WHERE rolname = %s""",
        (RUNTIME_ROLE,),
    ).fetchone()
    if row is None:
        return [
            "it does not exist: the migration user needs CREATEROLE, or a DBA creates it "
            "(deploy/README.md «Роли PostgreSQL»)"
        ]
    oid, *flags = row
    names = ("SUPERUSER", "CREATEDB", "CREATEROLE", "REPLICATION", "BYPASSRLS")
    problems = [name for name, value in zip(names, flags, strict=True) if value]
    problems += [
        f"member of {parent}"
        for (parent,) in connection.execute(
            """SELECT parent.rolname FROM pg_auth_members AS membership
               JOIN pg_roles AS parent ON parent.oid = membership.roleid
               WHERE membership.member = %s ORDER BY 1""",
            (oid,),
        )
    ]
    owned = connection.execute(
        """SELECT (SELECT count(*) FROM pg_class
                   WHERE relowner = %(oid)s AND relpersistence <> 't')
                + (SELECT count(*) FROM pg_namespace WHERE nspowner = %(oid)s)
                + (SELECT count(*) FROM pg_database WHERE datdba = %(oid)s)""",
        {"oid": oid},
    ).fetchone()[0]
    if owned:
        problems.append(f"owns {owned} objects")
    creates = connection.execute(
        """SELECT has_database_privilege(%(role)s, current_database(), 'CREATE'),
                  coalesce(has_schema_privilege(%(role)s, current_schema(), 'CREATE'), false)""",
        {"role": RUNTIME_ROLE},
    ).fetchone()
    if creates[0]:
        problems.append("CREATE on the database")
    if creates[1]:
        problems.append("CREATE on the schema")
    return problems


def runtime_dsn(owner_dsn: str, password: str) -> str:
    """The owner DSN (host, database, options) with the runtime role and its password."""
    params = conninfo_to_dict(owner_dsn)
    params.update(user=RUNTIME_ROLE, password=password)
    return make_conninfo(**params)


def check_runtime_login(owner_dsn: str, password: str) -> dict:
    """Log in as the runtime role once; what the session may do."""
    with psycopg.connect(runtime_dsn(owner_dsn, password), connect_timeout=10) as connection:
        user, superuser, schema, can_create = connection.execute(
            """SELECT current_user, rolsuper, current_schema(),
                      coalesce(has_schema_privilege(current_schema(), 'CREATE'), false)
               FROM pg_roles WHERE rolname = current_user"""
        ).fetchone()
    return {"user": user, "superuser": superuser, "schema": schema, "can_create": can_create}


def migrate(dsn: str, directory: Path, password: str | None) -> list[str]:
    """Migrations, then LOGIN of the runtime role when a password is given; log lines."""
    lines = [f"Applied {apply_migrations(dsn, directory)} operational migrations"]
    if not password:
        lines.append(f"INFRA_DB_APP_PASSWORD is not set: LOGIN of {RUNTIME_ROLE} left unchanged")
        return lines
    with psycopg.connect(dsn, autocommit=True) as connection:
        # A privileged or missing role never gets LOGIN from here.
        problems = runtime_role_problems(connection)
        if problems:
            raise RuntimeError(f"role {RUNTIME_ROLE} is not ready: {'; '.join(problems)}")
        enable_runtime_login(connection, password)
    state = check_runtime_login(dsn, password)
    if state["user"] != RUNTIME_ROLE or state["superuser"] or state["can_create"]:
        raise RuntimeError(f"unexpected session of {RUNTIME_ROLE}: {state}")
    lines.append(
        f"Runtime role {RUNTIME_ROLE}: LOGIN set, login checked "
        f"(superuser: no, CREATE in schema {state['schema']}: no)"
    )
    return lines


def main(directory: Path | None = None) -> int:
    dsn = os.environ.get("INFRA_DB_DSN")
    if not dsn:
        raise SystemExit("INFRA_DB_DSN (the schema owner) is required")
    folder = directory or Path(os.environ.get("INFRA_MIGRATIONS_DIR", DEFAULT_MIGRATIONS_DIR))
    try:
        lines = migrate(dsn, folder, os.environ.get("INFRA_DB_APP_PASSWORD") or None)
    except (RuntimeError, ValueError) as error:
        print(f"migrate: ERROR: {error}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

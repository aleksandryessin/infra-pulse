"""PostgreSQL role of the application code in the database tests (``make check-db``).

A fixture creates its schema and applies the migrations with the DSN user (the owner),
then hands the application ``app_dsn(dsn)``. ``INFRA_TEST_DB_ROLE`` selects the role:

* ``runtime`` (default): ``-crole=infra_pulse_app`` is added to the libpq options, so
  the API, the worker, the admin CLI and the tests' own queries run with the
  privileges that ``9000_runtime_role.sql`` grants (the stand's api and worker). No
  password is set on the cluster-wide role: the DSN user must be a superuser or a
  member of it. Loaders and seeds of the stand run as the owner and keep the owner DSN.
* ``owner``: the DSN user itself, as before the runtime role (29.09.2026).
"""

import os

from psycopg.conninfo import conninfo_to_dict, make_conninfo

from infra_pulse_backend.storage.migrations_pg import RUNTIME_ROLE

ROLE_OPTION = f"-crole={RUNTIME_ROLE}"


def db_role() -> str:
    value = os.environ.get("INFRA_TEST_DB_ROLE", "runtime")
    if value not in ("runtime", "owner"):
        raise ValueError("INFRA_TEST_DB_ROLE must be runtime or owner")
    return value


def app_dsn(dsn: str) -> str:
    """The DSN the application code of a test uses (see module docstring)."""
    if db_role() == "owner":
        return dsn
    params = conninfo_to_dict(dsn)
    params["options"] = f"{params.get('options', '')} {ROLE_OPTION}".strip()
    return make_conninfo(**params)


def owner_dsn(dsn: str) -> str:
    """The same DSN as the owner: for loaders, seeds and migrations inside a test."""
    params = conninfo_to_dict(dsn)
    options = " ".join(part for part in params.get("options", "").split() if part != ROLE_OPTION)
    params["options"] = options or None
    return make_conninfo(**params)

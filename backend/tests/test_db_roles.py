"""Static checks of the runtime PostgreSQL role (SEC-03, SEC-04); no database needed.

``backend/migrations/9000_runtime_role.sql`` must hold exactly one privilege decision
per relation the migrations create in the operational schema, so a new table cannot
reach the stand without an explicit decision. History tables stay append-only for the
runtime role. api and worker connect as ``infra_pulse_app`` in both Compose files;
migrations, backups and one-off loads keep the owner ``POSTGRES_USER``. The database
side (actual grants, refusals, a real login) is ``test_db_roles_db.py``.
"""

import re
from pathlib import Path

from infra_pulse_backend.storage.migrations_pg import PASSWORD_PATTERN, RUNTIME_ROLE

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "backend" / "migrations"
ROLE_FILE = MIGRATIONS / "9000_runtime_role.sql"
DML = {"SELECT", "INSERT", "UPDATE", "DELETE"}
APPEND_ONLY = {
    "audit_events",
    "forecast_decisions",
    "forecast_check_results",
    "work_order_drafts",
    "replay_review_audit",
}
CREATED = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:UNLOGGED\s+)?(?:TABLE|VIEW|MATERIALIZED\s+VIEW|SEQUENCE)"
    r"\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_.]*)",
    re.IGNORECASE,
)


def decisions() -> dict[str, str]:
    text = ROLE_FILE.read_text(encoding="utf-8")
    pairs = re.findall(r"\['([a-z_]+)',\s*'([^']*)'\]", text)
    result = dict(pairs)
    assert len(result) == len(pairs), "a relation has two decisions"
    return result


def operational_relations() -> set[str]:
    """Relations the migrations create in the current schema (not ``analytics.*``)."""
    names = set()
    for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        text = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
        for name in CREATED.findall(text):
            if "." not in name:
                names.add(name.lower())
    return names


def privileges(value: str) -> set[str]:
    return {part.split(" ")[0] for part in re.split(r",\s*(?![^()]*\))", value) if part}


def test_role_file_is_applied_after_every_schema_migration():
    files = sorted(path.name for path in MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    assert files[-1] == ROLE_FILE.name
    assert f"'{RUNTIME_ROLE}'" in ROLE_FILE.read_text(encoding="utf-8")


def test_every_table_has_an_explicit_runtime_decision():
    created = operational_relations()
    matrix = decisions()
    assert created, "no CREATE TABLE found: the parser is broken"
    missing = sorted(created - matrix.keys())
    unknown = sorted(matrix.keys() - created)
    assert not missing, f"add a line to 9000_runtime_role.sql for: {missing}"
    assert not unknown, f"decisions for relations no migration creates: {unknown}"


def test_decisions_grant_only_dml():
    for relation, value in decisions().items():
        assert privileges(value) <= DML, relation
        assert "TRUNCATE" not in value and "REFERENCES" not in value and "TRIGGER" not in value


def test_history_is_append_only_for_the_runtime_role():
    matrix = decisions()
    for relation in APPEND_ONLY:
        assert privileges(matrix[relation]) == {"SELECT", "INSERT"}, relation
    # The worker completes a missing alarm flag; the text of a record is never updated.
    assert matrix["dispatch_observations"] == "SELECT, INSERT, UPDATE (alarm)"


def test_password_pattern_is_url_safe():
    assert PASSWORD_PATTERN.fullmatch("0123456789abcdef" * 4)
    for bad in ("short", "a" * 23, "has space" * 4, "p@ss:word/" * 4, "a" * 129):
        assert not PASSWORD_PATTERN.fullmatch(bad)


def service_block(compose: str, service: str) -> str:
    """Lines of one top-level service (two-space indent) of a Compose file."""
    match = re.search(rf"^  {re.escape(service)}:\n((?:(?:    .*|\s*)\n)+)", compose, re.M)
    assert match, service
    return match.group(1)


def dsn_lines(block: str) -> list[str]:
    return [
        line.strip()
        for line in block.splitlines()
        if re.match(r"\s+(INFRA_DB_DSN|INFRA_REPLAY_DSN|INFRA_RECEIVED_DSN|PGUSER):", line)
    ]


def test_compose_runtime_services_use_the_runtime_role():
    for name in ("compose.yaml", "deploy/compose.server.yaml"):
        compose = (ROOT / name).read_text(encoding="utf-8")
        for service in ("api", "worker"):
            lines = dsn_lines(service_block(compose, service))
            assert lines, (name, service)
            for line in lines:
                assert f"://{RUNTIME_ROLE}:${{INFRA_DB_APP_PASSWORD" in line, (name, service)
                # One variable in the .env rolls both back to the owner DSN.
                assert line.startswith("INFRA_DB_DSN: ${INFRA_DB_APP_DSN:-"), (name, service)
                assert "POSTGRES_PASSWORD" not in line, (name, service)


def test_compose_owner_services_keep_the_owner():
    server = (ROOT / "deploy/compose.server.yaml").read_text(encoding="utf-8")
    for service in ("migrate", "replay-loader", "received-watcher", "backup"):
        lines = dsn_lines(service_block(server, service))
        assert lines and all("POSTGRES_USER" in line for line in lines), service
    migrate = service_block(server, "migrate")
    assert "INFRA_DB_APP_PASSWORD: ${INFRA_DB_APP_PASSWORD:?" in migrate
    local = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert all("POSTGRES_USER" in line for line in dsn_lines(service_block(local, "migrate")))


def test_server_env_template_names_the_runtime_password():
    template = (ROOT / "deploy/.env.server.example").read_text(encoding="utf-8")
    assert re.search(r"^INFRA_DB_APP_PASSWORD=__SET_ON_SERVER__$", template, re.M)
    assert re.search(r"^# INFRA_DB_APP_DSN=", template, re.M)

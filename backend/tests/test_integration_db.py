"""PostgreSQL path of the observation API (B1x): tokens, batch register, worker, audit.

Runs only with ``INFRA_TEST_RECEIVED_DSN`` pointing to a disposable database
(``make check-db``); every test works in its own schema, dropped after it. All records
are synthetic.
"""

import hashlib
import io
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from app_role import app_dsn, owner_dsn
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from infra_pulse_backend.admin.__main__ import main as admin_main
from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.api.auth_deps import AuthRuntime
from infra_pulse_backend.auth.ldap import DirectoryUser, InvalidCredentials
from infra_pulse_backend.auth.sessions import CSRF_COOKIE, CSRF_HEADER, PgAuthStore
from infra_pulse_backend.auth.throttle import LoginThrottle
from infra_pulse_backend.auth.tokens import PgTokenStore, TokenRateLimiter
from infra_pulse_backend.config import Settings
from infra_pulse_backend.ingestion.imports_pg import new_import_id, store_upload
from infra_pulse_backend.storage import integration_pg
from infra_pulse_backend.storage.audit_pg import AuditEvent
from infra_pulse_backend.worker import __main__ as worker_main
from infra_pulse_backend.worker.runner import WorkerConfig, recompute_stub, run_once
from infra_pulse_core.contracts.imports import ImportFile

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
NAMESPACE = "b1x-test"
STREAM = "b1x-stream"
CHANNELS_HEADER = (
    '"ид_канала_данных","тип_инж_системы","тип_датчика","тег_инженерной_системы",'
    '"название_датчика","ид_объект"'
)


@pytest.fixture
def dsn():
    base = os.environ.get("INFRA_TEST_RECEIVED_DSN")
    if not base:
        pytest.skip("local PostgreSQL integration DSN not provided")
    schema = f"b1x_{uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
    scoped = make_conninfo(base, options=f"-csearch_path={schema}")
    worker_main.apply_migrations(scoped, MIGRATIONS)
    try:
        yield app_dsn(scoped)
    finally:
        with psycopg.connect(base, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def settings(dsn, tmp_path) -> Settings:
    return Settings(
        mode="received",
        db_dsn=dsn,
        upload_dir=tmp_path / "uploads",
        received_namespace=NAMESPACE,
        received_stream_id=STREAM,
        _env_file=None,
    )


def worker(settings: Settings) -> WorkerConfig:
    return WorkerConfig(
        dsn=settings.db_dsn.get_secret_value(),
        upload_dir=settings.upload_dir,
        namespace_id=NAMESPACE,
        stream_id=STREAM,
        worker_id="worker-b1x",
        lease_seconds=60,
        retry_seconds=0,
    )


def drain(settings: Settings, recompute=recompute_stub) -> int:
    processed = 0
    while run_once(worker(settings), recompute):
        processed += 1
    return processed


def scalar(dsn: str, query: str, params: tuple = ()):
    with psycopg.connect(dsn) as connection:
        return connection.execute(query, params).fetchone()[0]


def rows(dsn: str, query: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(dsn) as connection:
        return connection.execute(query, params).fetchall()


def body(records: list, batch_id: str) -> bytes:
    return json.dumps({"batch_id": batch_id, "records": records}, ensure_ascii=False).encode()


def record(event_id, channel, at: str, alarm: bool, value: str) -> dict:
    date, time = at.split(" ")
    return {
        "event_id": event_id,
        "channel_id": channel,
        "date": date,
        "time": time,
        "alarm": alarm,
        "value": value,
    }


def load_channels(settings: Settings, mapping: dict[str, str]) -> None:
    """Reference CSV through the B1 path (queued import + worker)."""
    lines = [CHANNELS_HEADER]
    for index, (channel, object_id) in enumerate(mapping.items(), start=1):
        lines.append(
            f'{channel},Электроснабжение,Состояние фазы,"t.{index}",Фаза {index},{object_id}'
        )
    data = ("\n".join(lines) + "\n").encode()
    queue(settings, data, fmt="reference_channels_csv")
    drain(settings)


def queue(settings: Settings, data: bytes, *, fmt: str = "journal_csv") -> str:
    from infra_pulse_backend.ingestion.imports_pg import insert_import

    import_id = new_import_id()
    sha, size = store_upload(io.BytesIO(data), settings.upload_dir, f"{import_id}.dat", len(data))
    with psycopg.connect(settings.db_dsn.get_secret_value()) as connection:
        insert_import(
            connection,
            import_id=import_id,
            format=fmt,
            file_name=f"{fmt}.csv",
            stored_name=f"{import_id}.dat",
            sha256=sha,
            size_bytes=size,
            uploaded_by="local-operator",
            store_seconds=0.0,
        )
    return import_id


def register(settings: Settings, data: bytes, *, client_id: str, batch_id: str, token_id=None):
    """What the router does after validation: store the body, then register it."""
    import_id = new_import_id()
    sha, size = store_upload(io.BytesIO(data), settings.upload_dir, f"{import_id}.json", len(data))
    return integration_pg.register_batch(
        settings.db_dsn.get_secret_value(),
        client_id=client_id,
        batch_id=batch_id,
        token_id=token_id,
        records=len(json.loads(data)["records"]),
        import_id=import_id,
        file_name=f"observations-{batch_id}.json",
        stored_name=f"{import_id}.json",
        sha256=sha,
        size_bytes=size,
        store_seconds=0.001,
        audit=AuditEvent(
            action="observations.received",
            actor_id=client_id,
            actor_role="integration",
            target_kind="import_file",
            target_id=import_id,
            request_id=f"req-{uuid4().hex}",
            details={"batch_id": batch_id, "sha256": sha},
        ),
    )


def test_migration_is_idempotent_and_extends_the_checks(dsn):
    worker_main.apply_migrations(owner_dsn(dsn), MIGRATIONS)
    worker_main.apply_migrations(owner_dsn(dsn), MIGRATIONS)
    checks = dict(
        rows(
            dsn,
            """SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
               WHERE conname IN (
                 'import_files_format_check', 'audit_events_actor_role_check',
                 'import_files_source_container_check'
               ) AND connamespace = current_schema()::regnamespace""",
        )
    )
    assert "journal_json" in checks["import_files_format_check"]
    # 0021: an XML batch is reported with the container 'xml'.
    assert "'xml'" in checks["import_files_source_container_check"]
    assert "'xlsx'" in checks["import_files_source_container_check"]
    assert "journal_csv" in checks["import_files_format_check"]
    assert "integration" in checks["audit_events_actor_role_check"]
    # Only digests: no column can hold a token.
    columns = {
        name
        for (name,) in rows(
            dsn,
            """SELECT column_name FROM information_schema.columns
               WHERE table_name = 'integration_tokens' AND table_schema = current_schema()""",
        )
    }
    assert columns == {
        "token_id",
        "name",
        "token_sha256",
        "created_by",
        "created_at",
        "revoked_at",
        "revoked_by",
        "last_used_at",
    }


def test_admin_cli_issues_lists_and_revokes_with_audit(dsn, monkeypatch, capsys):
    monkeypatch.setenv("INFRA_DB_DSN", dsn)
    monkeypatch.chdir(Path(__file__).parent)  # no stray .env
    created = ["--actor", "admin-b1x", "token", "create", "--name", "scada-1", "--json"]
    assert admin_main(created) == 0
    issued = json.loads(capsys.readouterr().out)
    token, token_id = issued["token"], issued["token_id"]
    assert token.startswith("ipk_") and issued["name"] == "scada-1"
    stored = rows(dsn, "SELECT token_sha256, created_by FROM integration_tokens")
    assert stored == [(hashlib.sha256(token.encode()).hexdigest(), "admin-b1x")]

    store = PgTokenStore(dsn)
    identity = store.authenticate(token)
    assert (identity.token_id, identity.name, identity.actor_id) == (
        token_id,
        "scada-1",
        "integration:scada-1",
    )
    assert scalar(dsn, "SELECT last_used_at IS NOT NULL FROM integration_tokens")
    assert store.authenticate(token[:-1] + ("A" if token[-1] != "A" else "B")) is None
    assert store.authenticate("not-a-token") is None

    assert admin_main(["token", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["token_id"] for item in listed] == [token_id]
    assert "token" not in listed[0] and listed[0]["last_used_at"] is not None

    assert admin_main(["--actor", "admin-b1x", "token", "revoke", token_id]) == 0
    assert "revoked" in capsys.readouterr().out
    assert store.authenticate(token) is None  # effective for the next request
    assert admin_main(["token", "revoke", token_id]) == 2
    assert admin_main(["token", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert admin_main(["token", "list", "--all", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["revoked_by"] == "admin-b1x"

    # Rotation: two live tokens of one name, both revoked by name.
    assert admin_main(["token", "create", "--name", "scada-2", "--json"]) == 0
    assert admin_main(["token", "create", "--name", "scada-2", "--json"]) == 0
    capsys.readouterr()
    assert admin_main(["token", "revoke", "--name", "scada-2"]) == 0
    assert capsys.readouterr().out.count("revoked") == 2
    assert admin_main(["token", "create", "--name", "Bad Name"]) == 2

    audit = rows(
        dsn,
        """SELECT action, actor_id, actor_role, target_kind, target_id, details
           FROM audit_events ORDER BY occurred_at, audit_id""",
    )
    actions = [row[0] for row in audit]
    assert actions.count("integration_token.created") == 3
    assert actions.count("integration_token.listed") == 3
    assert actions.count("integration_token.revoked") == 3
    first = audit[0]
    assert first[:5] == (
        "integration_token.created",
        "admin-b1x",
        "admin",
        "integration_token",
        token_id,
    )
    assert first[5] == {"name": "scada-1"}
    assert all(token not in json.dumps(row[5]) for row in audit)


def test_json_batch_goes_through_the_b1_worker(settings, dsn):
    load_channels(settings, {"700001": "5001", "700002": "5002"})
    records = [
        record("9000001", "700001", "2026-09-20 03:09:27", False, "28"),
        record("9000002", "700002", "2026-09-20 10:00:00", True, "Неисправен"),
        record(None, "799999", "2026-09-20 11:00:00", False, "Обрыв; ТО"),
        record("9000004", "700001", "2026-09-20 25:00:00", False, "bad time"),
        {
            "channel_id": "700001",
            "event_at": "2026-09-20T12:00:00+03:00",
            "alarm": False,
            "value": "1,5",
        },
    ]
    row = register(settings, body(records, "b-1"), client_id="integration:scada-1", batch_id="b-1")
    assert (row["format"], row["status"], row["uploaded_by"]) == (
        "journal_json",
        "queued",
        "integration:scada-1",
    )
    assert scalar(dsn, "SELECT state FROM jobs WHERE import_id = %s", (row["import_id"],)) == (
        "queued"
    )
    audit = rows(
        dsn,
        "SELECT action, actor_id, actor_role FROM audit_events WHERE target_id = %s",
        (row["import_id"],),
    )
    assert audit == [("observations.received", "integration:scada-1", "integration")]

    assert drain(settings) == 1
    done = rows(
        dsn,
        """SELECT status, rows_total, rows_accepted, rows_duplicate, rows_quarantined,
                  unknown_channels, quarantine_reasons, event_from, event_to
           FROM import_files WHERE import_id = %s""",
        (row["import_id"],),
    )[0]
    assert done[:7] == ("imported", 5, 4, 0, 1, 1, {"bad_time": 1})
    assert done[7] == datetime(2026, 9, 20, 0, 9, 27, tzinfo=UTC)
    assert done[8] == datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
    stored = {
        value: (object_id, alarm, source_file, local_raw)
        for value, object_id, alarm, source_file, local_raw in rows(
            dsn,
            """SELECT value_raw, object_id, alarm, source_file, event_local_raw
               FROM dispatch_observations""",
        )
    }
    assert stored == {
        "28": ("5001", False, "observations-b-1.json", "2026-09-20 03:09:27"),
        "Неисправен": ("5002", True, "observations-b-1.json", "2026-09-20 10:00:00"),
        "Обрыв; ТО": (None, False, "observations-b-1.json", "2026-09-20 11:00:00"),
        "1,5": ("5001", False, "observations-b-1.json", "2026-09-20T12:00:00+03:00"),
    }
    assert rows(
        dsn,
        "SELECT line_no, reason FROM import_quarantine WHERE import_id = %s",
        (row["import_id"],),
    ) == [(4, "bad_time")]


def test_records_already_loaded_from_csv_or_another_batch_are_duplicates(settings, dsn):
    load_channels(settings, {"700001": "5001"})
    csv = (
        '"ид_события","ид_канала_данных","дата","время","тревожное","значение_датчика"\n'
        '1,700001,"2026-09-20","00:00:01",false,"1.5"\n'
        '2,700001,"2026-09-20","00:00:02",true,"Неисправен"\n'
    ).encode()
    queue(settings, csv)
    drain(settings)
    overlap = [
        record("1", "700001", "2026-09-20 00:00:01", False, "1.5"),
        {
            "event_id": "2",
            "channel_id": "700001",
            "event_at": "2026-09-19T21:00:02Z",
            "alarm": True,
            "value": "Неисправен",
        },
        record("3", "700001", "2026-09-20 00:00:03", False, "Норма"),
    ]
    first = register(settings, body(overlap, "b-1"), client_id="integration:a", batch_id="b-1")
    second = register(settings, body(overlap, "b-2"), client_id="integration:a", batch_id="b-2")
    drain(settings)
    counts = rows(
        dsn,
        """SELECT import_id, rows_accepted, rows_duplicate FROM import_files
           WHERE import_id IN (%s, %s) ORDER BY seq""",
        (first["import_id"], second["import_id"]),
    )
    assert [count[1:] for count in counts] == [(1, 2), (0, 3)]
    assert scalar(dsn, "SELECT count(*) FROM dispatch_observations") == 3


def test_batch_key_is_registered_once(settings, dsn):
    data = body([record("1", "700001", "2026-09-20 00:00:01", False, "a")], "b-1")
    first = register(settings, data, client_id="integration:a", batch_id="b-1")
    with pytest.raises(integration_pg.BatchRaced):
        register(settings, data, client_id="integration:a", batch_id="b-1")
    # The loser wrote nothing: one import, one job, one audit row.
    assert scalar(dsn, "SELECT count(*) FROM import_files") == 1
    assert scalar(dsn, "SELECT count(*) FROM jobs") == 1
    assert scalar(dsn, "SELECT count(*) FROM audit_events") == 1
    found = integration_pg.find_batch(
        settings.db_dsn.get_secret_value(), client_id="integration:a", batch_id="b-1"
    )
    assert found == integration_pg.StoredBatch(first["import_id"], first["sha256"])
    # Another client may use the same batch ID.
    other = register(settings, data, client_id="integration:b", batch_id="b-1")
    assert other["import_id"] != first["import_id"]
    assert integration_pg.batch_owner(settings.db_dsn.get_secret_value(), other["import_id"]) == (
        "integration:b"
    )


def test_json_batch_is_recomputed_like_a_csv_day(settings, dsn):
    load_channels(settings, {"700001": "5001"})
    calls = []

    def publish(connection, *, data_as_of, import_id):
        calls.append((data_as_of, import_id))
        return SimpleNamespace(generation=3, new_card_ids=[], released_card_ids=["card-7"])

    row = register(
        settings,
        body([record("1", "700001", "2026-09-20 05:00:00", True, "a")], "b-1"),
        client_id="integration:a",
        batch_id="b-1",
    )
    drain(settings, recompute=publish)
    assert calls == [(datetime(2026, 9, 20, 2, 0, tzinfo=UTC), row["import_id"])]
    assert rows(
        dsn,
        """SELECT status, forecast_generation, released_card_ids FROM import_files
           WHERE import_id = %s""",
        (row["import_id"],),
    ) == [("published", 3, ["card-7"])]


# --- HTTP path (after C0.2: router api/observations.py, contract ObservationBatch) ---


class Directory:
    """Directory stand-in: a synthetic administrator and a dispatcher."""

    USERS = {"admin-b1x": "admin", "disp-b1x": "dispatcher"}

    def authenticate(self, username: str, password: str) -> DirectoryUser:
        if username not in self.USERS or password != "synthetic-password-1":
            raise InvalidCredentials
        return DirectoryUser(username, f"Тест {username}", frozenset({self.USERS[username]}))


def app_client(settings: Settings, **overrides) -> TestClient:
    app = create_app(settings.model_copy(update=overrides))
    if overrides.get("auth_mode") == "ldap":
        dsn = settings.db_dsn.get_secret_value()
        app.state.auth_runtime = AuthRuntime(
            store=PgAuthStore(dsn),
            directory=Directory(),
            throttle=LoginThrottle(5, 300),
            tokens=PgTokenStore(dsn),
            token_limiter=TokenRateLimiter(overrides.get("integration_requests_per_minute", 60)),
        )
    return TestClient(app, base_url="https://testserver")


def issue(dsn: str, name: str = "scada-1") -> str:
    token, _ = integration_pg.create_token(dsn, name=name, actor="admin-b1x", request_id="req-t")
    return token


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def login(client: TestClient, username: str) -> dict:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "synthetic-password-1"},
        headers={CSRF_HEADER: "login"},
    )
    assert response.status_code == 200, response.text
    return {CSRF_HEADER: client.cookies[CSRF_COOKIE]}


def test_post_is_queued_retry_is_200_and_other_bytes_are_409(settings, dsn):
    load_channels(settings, {"700001": "5001", "700002": "5002"})
    token = issue(dsn)
    client = app_client(settings)
    records = [
        record("9000001", "700001", "2026-09-20 03:09:27", False, "28"),
        record("9000002", "700002", "2026-09-20 10:00:00", True, "Неисправен"),
        record("9000003", "799999", "2026-02-30 11:00:00", False, "плохая дата"),
    ]
    data = body(records, "b-1")
    response = client.post("/api/v1/observations", content=data, headers=bearer(token))
    assert response.status_code == 202, response.text
    queued = ImportFile.model_validate(response.json())
    assert (queued.status, queued.format, queued.uploaded_by, queued.simulated) == (
        "queued",
        "journal_json",
        "integration:scada-1",
        False,
    )
    assert queued.sha256 == hashlib.sha256(data).hexdigest()
    assert queued.size_bytes == len(data)
    assert (settings.upload_dir / f"{queued.id}.json").read_bytes() == data

    again = client.post("/api/v1/observations", content=data, headers=bearer(token))
    assert again.status_code == 200, again.text
    assert again.json()["id"] == queued.id
    other = body(records[:1], "b-1")
    conflict = client.post("/api/v1/observations", content=other, headers=bearer(token))
    assert (conflict.status_code, conflict.json()["detail"]) == (409, "batch_id_conflict")
    assert sorted(path.name for path in settings.upload_dir.glob("*.json")) == [f"{queued.id}.json"]
    assert scalar(dsn, "SELECT count(*) FROM import_files WHERE format = 'journal_json'") == 1
    [audit] = rows(
        dsn,
        """SELECT action, actor_id, actor_role, target_kind, details FROM audit_events
           WHERE action = 'observations.received'""",
    )
    assert audit[:4] == (
        "observations.received",
        "integration:scada-1",
        "integration",
        "import_file",
    )
    assert audit[4]["records"] == 3 and audit[4]["sha256"] == queued.sha256
    assert audit[4]["batch_id"] == "b-1" and audit[4]["token_id"].startswith("tok-")

    drain(settings)
    status = client.get(f"/api/v1/observations/{queued.id}", headers=bearer(token))
    assert status.status_code == 200, status.text
    done = ImportFile.model_validate(status.json())
    assert (done.status, done.rows_accepted, done.rows_quarantined) == ("imported", 2, 1)
    assert [(item.line_no, item.reason) for item in done.quarantine_sample] == [(3, "bad_date")]
    # The stored report is what a retry returns now.
    assert (
        client.post("/api/v1/observations", content=data, headers=bearer(token)).json()["status"]
        == "imported"
    )


def test_token_is_checked_and_grants_ingest_only(settings, dsn):
    token = issue(dsn)
    other = issue(dsn, "scada-2")
    data = body([record("1", "700001", "2026-09-20 00:00:01", False, "a")], "b-1")
    for mode in ("dev_stub", "ldap"):
        client = app_client(settings, auth_mode=mode)
        json_type = {"Content-Type": "application/json"}
        for headers in (
            bearer("ipk_" + "x" * 43),
            {"Authorization": "Bearer", **json_type},
            {"Authorization": "Bearer not-a-token", **json_type},
        ):
            response = client.post("/api/v1/observations", content=data, headers=headers)
            assert (response.status_code, response.json()["detail"]) == (401, "invalid_token")
            assert response.headers["www-authenticate"] == "Bearer"
        me = client.get("/api/v1/auth/me", headers=bearer(token)).json()
        assert (me["subject_id"], me["roles"], me["auth_source"]) == (
            "integration:scada-1",
            ["integration"],
            "token",
        )
        for method, path in (
            ("GET", "/api/v1/attention"),
            ("GET", "/api/v1/forecast-state"),
            ("GET", "/api/v1/imports"),
            ("POST", "/api/v1/imports"),
            ("GET", "/api/v1/research-summary"),
            ("POST", "/api/v1/forecasts/synthetic-feeders-14d-011/decisions"),
        ):
            denied = client.request(method, path, headers=bearer(token))
            assert (denied.status_code, denied.json()["detail"]) == (403, "forbidden_role"), path

    client = app_client(settings, auth_mode="ldap")
    json_type = {"Content-Type": "application/json"}
    assert client.post("/api/v1/observations", content=data, headers=json_type).status_code == 401
    accepted = client.post("/api/v1/observations", content=data, headers=bearer(token))
    assert accepted.status_code == 202, accepted.text  # no cookie, no CSRF header
    import_id = accepted.json()["id"]
    # Another integration sees neither the batch nor its batch ID namespace.
    assert client.get(f"/api/v1/observations/{import_id}", headers=bearer(other)).status_code == 404
    assert (
        client.post("/api/v1/observations", content=data, headers=bearer(other)).status_code == 202
    )

    # Revocation applies to the next request; last_used_at was stamped.
    assert scalar(dsn, "SELECT count(*) FROM integration_tokens WHERE last_used_at IS NULL") == 0
    integration_pg.revoke_tokens(dsn, actor="admin-b1x", request_id="req-r", name="scada-1")
    revoked = client.get(f"/api/v1/observations/{import_id}", headers=bearer(token))
    assert (revoked.status_code, revoked.json()["detail"]) == (401, "invalid_token")


def test_directory_sessions_need_admin_and_csrf(settings, dsn):
    data = body([record("1", "700001", "2026-09-20 00:00:01", False, "a")], "b-admin")
    dispatcher = app_client(settings, auth_mode="ldap")
    csrf = login(dispatcher, "disp-b1x")
    denied = dispatcher.post(
        "/api/v1/observations", content=data, headers={**csrf, "Content-Type": "application/json"}
    )
    assert (denied.status_code, denied.json()["detail"]) == (403, "forbidden_role")

    admin = app_client(settings, auth_mode="ldap")
    csrf = login(admin, "admin-b1x")
    json_type = {"Content-Type": "application/json"}
    no_csrf = admin.post("/api/v1/observations", content=data, headers=json_type)
    assert (no_csrf.status_code, no_csrf.json()["detail"]) == (403, "csrf_failed")
    accepted = admin.post("/api/v1/observations", content=data, headers={**csrf, **json_type})
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["uploaded_by"] == "admin-b1x"
    assert rows(
        dsn,
        "SELECT actor_id, actor_role FROM audit_events WHERE action = 'observations.received'",
    ) == [("admin-b1x", "admin")]
    assert admin.get(f"/api/v1/observations/{accepted.json()['id']}").status_code == 200


def test_limits_validation_rate_and_outage(settings, dsn):
    token = issue(dsn)
    client = app_client(settings, integration_requests_per_minute=3)
    good = record("1", "700001", "2026-09-20 00:00:01", False, "a")

    too_many = body([good] * 5001, "b-many")
    response = client.post("/api/v1/observations", content=too_many, headers=bearer(token))
    assert (response.status_code, response.json()["detail"]) == (413, "too_many_records")

    invalid = body([good, {**good, "alarm": "t"}, {**good, "extra": 1}], "b-bad")
    response = client.post("/api/v1/observations", content=invalid, headers=bearer(token))
    assert response.status_code == 422
    assert sorted({error.get("record") for error in response.json()["detail"]}) == [2, 3]

    huge = b'{"batch_id": "b-huge", "records": [' + b" " * (5 * 1024 * 1024) + b"]}"
    response = client.post("/api/v1/observations", content=huge, headers=bearer(token))
    assert (response.status_code, response.json()["detail"]) == (413, "batch_too_large")
    # Rate limit (3 a minute): the oversized body was refused before authentication, so
    # two requests are counted above; the third is accepted and the fourth is 429.
    third = client.post("/api/v1/observations", content=body([good], "b-3"), headers=bearer(token))
    assert third.status_code == 202, third.text
    limited = client.post(
        "/api/v1/observations", content=body([good], "b-4"), headers=bearer(token)
    )
    assert (limited.status_code, limited.json()["detail"]) == (429, "rate_limited")
    assert 1 <= int(limited.headers["retry-after"]) <= 61
    # Only the accepted batch was stored: refusals leave no import, file or audit row.
    assert scalar(dsn, "SELECT count(*) FROM import_files") == 1
    assert scalar(dsn, "SELECT count(*) FROM audit_events") == 2  # token issue + batch
    assert len(list(settings.upload_dir.glob("*.json"))) == 1

    # Storage down: the token cannot be checked (fail closed), an admin gets 503 too.
    down = app_client(settings, db_dsn=SecretStr("postgresql://nobody@127.0.0.1:1/none"))
    outage = down.post("/api/v1/observations", content=body([good], "b-5"), headers=bearer(token))
    assert (outage.status_code, outage.json()["detail"]) == (503, "auth_storage_unavailable")
    outage = down.post(
        "/api/v1/observations",
        content=body([good], "b-5"),
        headers={"Content-Type": "application/json"},
    )
    assert (outage.status_code, outage.json()["detail"]) == (503, "imports_storage_unavailable")


def xml_body(records: list[dict], batch_id: str) -> bytes:
    """The same batch in XML (docs/INTEGRATION_API.md): fields as child elements."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f'<batch batch_id="{batch_id}">']
    for item in records:
        fields = "".join(
            f"<{name}>{str(value).lower() if isinstance(value, bool) else value}</{name}>"
            for name, value in item.items()
            if value is not None
        )
        lines.append(f"  <record>{fields}</record>")
    return ("\n".join([*lines, "</batch>"]) + "\n").encode()


def xml_bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/xml"}


def test_xml_batch_reaches_the_same_records_as_json(settings, dsn):
    load_channels(settings, {"700001": "5001", "700002": "5002"})
    token = issue(dsn)
    client = app_client(settings)
    records = [
        record("9000001", "700001", "2026-09-20 03:09:27", False, "28"),
        record("9000002", "700002", "2026-09-20 10:00:00", True, "Неисправен"),
        record("9000003", "799999", "2026-09-20 11:00:00", False, " Обрыв; ТО "),
        record("9000004", "700001", "2026-02-30 11:00:00", False, "плохая дата"),
        {
            "event_id": "9000005",
            "channel_id": "700001",
            "event_at": "2026-09-20T12:00:00+03:00",
            "alarm": False,
            "value": "1,5",
        },
    ]
    data = xml_body(records, "b-xml")
    response = client.post("/api/v1/observations", content=data, headers=xml_bearer(token))
    assert response.status_code == 202, response.text
    queued = ImportFile.model_validate(response.json())
    assert (queued.format, queued.status, queued.file_name) == (
        "journal_json",
        "queued",
        "observations-b-xml.xml",
    )
    # Kept as sent: the XML bytes, their SHA-256 and size.
    assert queued.sha256 == hashlib.sha256(data).hexdigest() and queued.size_bytes == len(data)
    assert (settings.upload_dir / f"{queued.id}.xml").read_bytes() == data
    [audit] = rows(
        dsn,
        "SELECT actor_id, details FROM audit_events WHERE action = 'observations.received'",
    )
    assert audit[0] == "integration:scada-1"
    assert (audit[1]["container"], audit[1]["records"], audit[1]["batch_id"]) == (
        "xml",
        5,
        "b-xml",
    )

    # Retry: the same batch_id and the same bytes -> 200 with the stored report.
    again = client.post("/api/v1/observations", content=data, headers=xml_bearer(token))
    assert (again.status_code, again.json()["id"]) == (200, queued.id)
    # The JSON spelling of the same batch is other bytes under the same batch_id -> 409,
    # and so is the same XML reformatted.
    for other, headers in (
        (body(records, "b-xml"), bearer(token)),
        (data.replace(b"\n  <record>", b"<record>"), xml_bearer(token)),
    ):
        conflict = client.post("/api/v1/observations", content=other, headers=headers)
        assert (conflict.status_code, conflict.json()["detail"]) == (409, "batch_id_conflict")

    assert drain(settings) == 1
    done = ImportFile.model_validate(
        client.get(f"/api/v1/observations/{queued.id}", headers=bearer(token)).json()
    )
    assert (done.status, done.source_layout, done.source_container) == (
        "imported",
        "api_batch",
        "xml",
    )
    assert (done.rows_total, done.rows_accepted, done.rows_quarantined) == (5, 4, 1)
    assert done.unknown_channels == 1
    assert [(item.line_no, item.reason) for item in done.quarantine_sample] == [(4, "bad_date")]
    stored = rows(
        dsn,
        """SELECT source_event_id, channel_id, event_at, alarm, value_raw, object_id, source_file,
                  event_local_raw
           FROM dispatch_observations ORDER BY event_at, source_event_id""",
    )
    assert [row[4] for row in stored] == ["28", "Неисправен", " Обрыв; ТО ", "1,5"]
    assert {row[6] for row in stored} == {"observations-b-xml.xml"}
    assert stored[1][:4] == ("9000002", "700002", datetime(2026, 9, 20, 7, 0, tzinfo=UTC), True)

    # The JSON form of the same records, as a new batch: every valid record is already
    # there (same overlap key), so the XML batch stored exactly what JSON would.
    as_json = client.post(
        "/api/v1/observations", content=body(records, "b-json"), headers=bearer(token)
    )
    assert as_json.status_code == 202, as_json.text
    drain(settings)
    twin = ImportFile.model_validate(
        client.get(f"/api/v1/observations/{as_json.json()['id']}", headers=bearer(token)).json()
    )
    assert (twin.source_container, twin.rows_accepted, twin.rows_duplicate) == ("json", 0, 4)
    assert twin.rows_quarantined == 1
    assert scalar(dsn, "SELECT count(*) FROM dispatch_observations") == 4


def test_xml_refusals_store_nothing(settings, dsn):
    token = issue(dsn)
    client = app_client(settings)
    good = record("1", "700001", "2026-09-20 00:00:01", False, "a")
    refused = [
        (
            b'<?xml version="1.0"?><!DOCTYPE batch [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
            b'<batch batch_id="b-dtd"><record><value>&b;</value></record></batch>',
            xml_bearer(token),
            422,
            "xml_forbidden",
        ),
        (
            b'<?xml version="1.0"?><!DOCTYPE batch [<!ENTITY x SYSTEM "file:///etc/hosts">]>'
            b'<batch batch_id="b-xxe"><record><value>&x;</value></record></batch>',
            xml_bearer(token),
            422,
            "xml_forbidden",
        ),
        (xml_body([good], "b-1").replace(b"<value>", b"<extra/><value>"), xml_bearer(token), 422,
         "extra_forbidden"),
        (xml_body([], "b-empty"), xml_bearer(token), 422, "too_short"),
        (xml_body([good], "b 1"), xml_bearer(token), 422, "string_pattern_mismatch"),
        (xml_body([good], "b-2"), {**xml_bearer(token), "Content-Type": "text/plain"}, 415,
         "unsupported_media_type"),
    ]  # fmt: skip
    for data, headers, status, kind in refused:
        response = client.post("/api/v1/observations", content=data, headers=headers)
        assert response.status_code == status, response.text
        detail = response.json()["detail"]
        assert (detail if isinstance(detail, str) else detail[0]["type"]) == kind
    too_many = xml_body([good] * 5001, "b-many")
    response = client.post("/api/v1/observations", content=too_many, headers=xml_bearer(token))
    assert (response.status_code, response.json()["detail"]) == (413, "too_many_records")
    # Refusals leave no import, file, job or audit row (only the token issue is audited).
    assert scalar(dsn, "SELECT count(*) FROM import_files") == 0
    assert scalar(dsn, "SELECT count(*) FROM jobs") == 0
    assert scalar(dsn, "SELECT count(*) FROM audit_events") == 1
    assert not settings.upload_dir.exists() or not any(settings.upload_dir.iterdir())


def test_5000_record_batch_is_imported_within_seconds(settings, dsn):
    """Measured locally; the numbers are printed for the hand-over (run with -s)."""
    channels = {str(800000 + index): str(6000 + index % 78) for index in range(1000)}
    load_channels(settings, channels)
    token = issue(dsn)
    client = app_client(settings)
    records = [
        record(
            str(7_000_000_000 + index),
            str(800000 + index % 1000),
            f"2026-09-27 {index // 3600 % 24:02d}:{index // 60 % 60:02d}:{index % 60:02d}",
            index % 97 == 0,
            "Неисправен" if index % 97 == 0 else str(index % 40),
        )
        for index in range(5000)
    ]
    data = body(records, "b-5000")
    started = time.perf_counter()
    response = client.post("/api/v1/observations", content=data, headers=bearer(token))
    posted = time.perf_counter()
    assert response.status_code == 202, response.text
    assert drain(settings) == 1
    finished = time.perf_counter()
    done = ImportFile.model_validate(
        client.get(f"/api/v1/observations/{response.json()['id']}", headers=bearer(token)).json()
    )
    assert (done.status, done.rows_accepted, done.rows_quarantined) == ("imported", 5000, 0)
    stages = {timing.stage: timing.seconds for timing in done.timings}
    print(
        f"\n5000 records, {len(data) / 1e6:.2f} MB: POST {posted - started:.3f}s, "
        f"worker {finished - posted:.3f}s, total {finished - started:.3f}s, stages {stages}"
    )
    assert finished - started < 10

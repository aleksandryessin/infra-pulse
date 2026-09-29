"""Upload of source CSV files. **Owned by package B1.**

HTTP only stores the file in ``INFRA_UPLOAD_DIR``, records ``import_files``,
``jobs`` and the ``import.uploaded`` audit row (actor, acting role, request id and
address from ``auth_deps``) in one transaction and answers 202; the worker
(``python -m infra_pulse_backend.worker``) parses, loads, recomputes and publishes.
Fixture mode returns simulated reports without storing anything; other modes need
``INFRA_DB_DSN``.
"""

import hashlib
from pathlib import Path
from time import perf_counter
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile

from infra_pulse_backend.api import forecast_fixture, product_fixture
from infra_pulse_backend.api.auth_deps import (
    acting_role,
    client_address,
    request_id,
    require_permission,
)
from infra_pulse_backend.config import Settings
from infra_pulse_backend.ingestion.imports_pg import (
    UploadTooLarge,
    create_import,
    list_imports,
    new_import_id,
    read_import,
    store_upload,
    stored_path,
)
from infra_pulse_backend.storage.audit_pg import AuditEvent
from infra_pulse_core.contracts.auth import Me
from infra_pulse_core.contracts.imports import (
    MAX_IMPORT_BYTES,
    ImportFile,
    ImportFormat,
    ImportList,
)


def _file_name(upload: UploadFile) -> str:
    """Base name only: the client path is never used for storage."""
    name = Path((upload.filename or "").replace("\\", "/")).name.strip()
    return (name or "upload.csv")[:255]


def build_router(config: Settings) -> APIRouter:
    router = APIRouter()
    fixture = config.mode == "fixture"

    def storage_dsn() -> str:
        if config.db_dsn is None:
            raise HTTPException(status_code=503, detail="imports_not_configured")
        return config.db_dsn.get_secret_value()

    @router.post("/api/v1/imports", response_model=ImportFile, status_code=202)
    def upload(
        file: Annotated[UploadFile, File()],
        actor: Annotated[Me, Depends(require_permission("import"))],
        request: Request,
        format: Annotated[ImportFormat, Form()] = "journal_csv",
    ) -> ImportFile:
        if fixture:
            content = file.file.read(MAX_IMPORT_BYTES + 1)
            if len(content) > MAX_IMPORT_BYTES:
                raise HTTPException(status_code=413, detail="file_too_large")
            return ImportFile(
                id="synthetic-import-new",
                format=format,
                file_name=(file.filename or "upload.csv")[:255],
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                uploaded_by=actor.subject_id,
                uploaded_at=forecast_fixture.FIXTURE_AS_OF,
                status="queued",
                simulated=True,
            )
        dsn = storage_dsn()
        import_id = new_import_id()
        stored_name = f"{import_id}.csv"
        started = perf_counter()
        try:
            sha256, size = store_upload(file.file, config.upload_dir, stored_name, MAX_IMPORT_BYTES)
        except UploadTooLarge as error:
            raise HTTPException(status_code=413, detail="file_too_large") from error
        except OSError as error:
            raise HTTPException(status_code=503, detail="upload_storage_unavailable") from error
        try:
            return create_import(
                dsn,
                import_id=import_id,
                format=format,
                file_name=_file_name(file),
                stored_name=stored_name,
                sha256=sha256,
                size_bytes=size,
                uploaded_by=actor.subject_id,
                store_seconds=perf_counter() - started,
                audit=AuditEvent(
                    action="import.uploaded",
                    actor_id=actor.subject_id,
                    actor_role=acting_role(actor, "import"),
                    target_kind="import_file",
                    target_id=import_id,
                    request_id=request_id(request),
                    client_address=client_address(request),
                    details={"sha256": sha256, "format": format, "size_bytes": size},
                ),
            )
        except psycopg.Error as error:
            stored_path(config.upload_dir, stored_name).unlink(missing_ok=True)
            raise HTTPException(status_code=503, detail="imports_storage_unavailable") from error

    @router.get(
        "/api/v1/imports",
        response_model=ImportList,
        dependencies=[Depends(require_permission("import"))],
    )
    def imports(
        cursor: str | None = Query(default=None, min_length=1, max_length=64),
        limit: int = Query(default=25, ge=1, le=100),
    ) -> ImportList:
        if fixture:
            if cursor is not None:
                raise HTTPException(status_code=422, detail="invalid_import_cursor")
            return product_fixture.import_list(limit)
        try:
            return list_imports(storage_dsn(), cursor=cursor, limit=limit)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="invalid_import_cursor") from error
        except psycopg.Error as error:
            raise HTTPException(status_code=503, detail="imports_storage_unavailable") from error

    @router.get(
        "/api/v1/imports/{import_id}",
        response_model=ImportFile,
        dependencies=[Depends(require_permission("import"))],
    )
    def import_file(import_id: str) -> ImportFile:
        if fixture:
            found = next((item for item in product_fixture.imports() if item.id == import_id), None)
        else:
            try:
                found = read_import(storage_dsn(), import_id)
            except psycopg.Error as error:
                raise HTTPException(
                    status_code=503, detail="imports_storage_unavailable"
                ) from error
        if found is None:
            raise HTTPException(status_code=404, detail="import_not_found")
        return found

    return router

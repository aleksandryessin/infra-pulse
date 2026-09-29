"""Streaming data through the API (ТЗ §7, §9, §10). **Owned by package B1x.**

``POST /api/v1/observations`` takes a batch of journal records (fields of the CSV
export, ``ObservationBatch``) as JSON (``Content-Type: application/json``) or XML
(``application/xml`` or ``text/xml``, ``ingestion/batch_xml.py``: ``<batch>`` with
``<record>`` elements, parsed without DTD or entities), stores the body **as sent** as an
upload of format ``journal_json`` (``<import>.json`` or ``<import>.xml``) and queues it
for the same worker as the CSV (202 ``ImportFile``, ``queued``). Both bodies are
validated by the same model, so limits and errors are the same. The worker parses it
(``ingestion/journal_json.py``), loads ``dispatch_observations`` with the CSV overlap
key and calls the forecast recompute. Another ``Content-Type`` or none is 415
``unsupported_media_type`` (before authentication, like 413). Responses are JSON.

Caller: an integration token ``Authorization: Bearer ipk_...`` (``auth/tokens.py``) or an
administrator session (permission ``ingest``). ``(caller, batch_id)`` is the idempotency
key: the same key with the same bytes is 200 with the stored import and no new write or
audit row; other bytes are 409 ``batch_id_conflict`` — so the XML and the JSON spelling
of one batch are different bodies, and a retry repeats the first one byte for byte.
Errors: 401 ``invalid_token`` (unknown or revoked), 403 ``forbidden_role``, 413
``batch_too_large`` (over 5 MiB, refused before the body is read to the end) or
``too_many_records`` (over 5 000), 415 ``unsupported_media_type``, 422 schema errors
with the 1-based ``record`` number (for XML also ``xml_invalid``, ``xml_forbidden`` for a
DTD or entities, ``xml_structure``, ``xml_duplicate_field``), 429 ``rate_limited`` with
``Retry-After`` (per token, default 60 batches a minute), 503 when storage is down or
not configured. ``GET /api/v1/observations/{import_id}`` is the status of the caller's
own batch (an administrator sees every batch; others are 404 ``batch_not_found``).
Fixture mode validates the batch and returns a simulated report without storing it.
Customer instructions: ``docs/INTEGRATION_API.md``.
"""

import hashlib
import io
import json
from collections.abc import Callable, Coroutine
from time import perf_counter
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from infra_pulse_backend.api import forecast_fixture, product_fixture
from infra_pulse_backend.api.auth_deps import (
    acting_role,
    auth_runtime,
    client_address,
    current_integration,
    request_id,
    require_permission,
)
from infra_pulse_backend.config import Settings
from infra_pulse_backend.ingestion.batch_xml import BatchXmlError, batch_from_xml
from infra_pulse_backend.ingestion.imports_pg import (
    new_import_id,
    read_import,
    store_upload,
    stored_path,
    to_model,
)
from infra_pulse_backend.storage.audit_pg import AuditEvent
from infra_pulse_backend.storage.integration_pg import (
    BatchRaced,
    StoredBatch,
    batch_owner,
    find_batch,
    register_batch,
)
from infra_pulse_core.contracts.auth import Me
from infra_pulse_core.contracts.imports import MAX_OBSERVATION_BYTES, ImportFile, ObservationBatch

STORAGE_DOWN = "imports_storage_unavailable"
JSON_TYPES = ("application/json",)
XML_TYPES = ("application/xml", "text/xml")
# Synthetic examples in OpenAPI (Swagger «Try it out»): one batch, JSON and XML spellings
# (backend/tests/test_api_docs.py checks both parse to the same ObservationBatch). Time
# 30.06.2026 about 12:00 MSK, inside the data of the stand: a later record would move the
# forecast time of the stand for everyone. Channels demo-700001...demo-700003 are not in
# the reference, and every record has alarm=false: a «Try it out» on the stand adds no
# alarm message to «Сейчас» or «Схема» (rehearsal 29.09.2026, P1-3).
JSON_EXAMPLE = {
    "batch_id": "scada-ods-1-20260630T120000-000184",
    "records": [
        {
            "event_id": "9000001",
            "channel_id": "demo-700001",
            "date": "2026-06-30",
            "time": "11:59:58",
            "alarm": False,
            "value": "28",
        },
        {
            "ид_события": "9000002",
            "ид_канала_данных": "demo-700002",
            "дата": "2026-06-30",
            "время": "11:59:59",
            "тревожное": False,
            "значение_датчика": "Неисправен",
        },
        {
            "event_id": "9000003",
            "channel_id": "demo-700003",
            "event_at": "2026-06-30T09:00:00Z",
            "alarm": False,
            "value": "Обрыв; ТО",
        },
    ],
}
XML_EXAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<batch batch_id="scada-ods-1-20260630T120000-000184">
  <record>
    <event_id>9000001</event_id>
    <channel_id>demo-700001</channel_id>
    <date>2026-06-30</date>
    <time>11:59:58</time>
    <alarm>false</alarm>
    <value>28</value>
  </record>
  <record>
    <ид_события>9000002</ид_события>
    <ид_канала_данных>demo-700002</ид_канала_данных>
    <дата>2026-06-30</дата>
    <время>11:59:59</время>
    <тревожное>false</тревожное>
    <значение_датчика>Неисправен</значение_датчика>
  </record>
  <record>
    <event_id>9000003</event_id>
    <channel_id>demo-700003</channel_id>
    <event_at>2026-06-30T09:00:00Z</event_at>
    <alarm>false</alarm>
    <value>Обрыв; ТО</value>
  </record>
</batch>
"""
BATCH_BODY = {
    "requestBody": {
        "description": (
            "ObservationBatch as JSON, or the same batch in XML: root <batch> with the "
            "batch_id attribute (or a <batch_id> element) and <record> elements whose "
            "child elements are the record fields; alarm is true or false. DTD and "
            "entities are refused (422 xml_forbidden). docs/INTEGRATION_API.md"
        ),
        "content": {
            "application/json": {"example": JSON_EXAMPLE},
            "application/xml": {
                "schema": {"$ref": "#/components/schemas/ObservationBatch"},
                "example": XML_EXAMPLE,
            },
        },
    }
}


def _too_large(code: str) -> JSONResponse:
    return JSONResponse(status_code=413, content={"detail": code})


def _body_container(content_type: str | None) -> str | None:
    """``json`` or ``xml`` by the media type; ``None`` (415) for any other or none.

    FastAPI itself never reads a body without ``Content-Type`` as JSON (strict content
    type), so such a request was a 422 before and is a 415 now.
    """
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media in JSON_TYPES or (media.startswith("application/") and media.endswith("+json")):
        return "json"
    if media in XML_TYPES:
        return "xml"
    return None


def _as_json(request: Request, data: dict) -> Request:
    """The request FastAPI validates for an XML body: the parsed batch as JSON.

    A copy of the ASGI scope with ``Content-Type: application/json`` and the batch as
    the cached body; the state (``batch_bytes``, the caller, the audit actor) is the
    same dict, so hashing, storage and audit see the XML bytes as sent.
    """
    headers = [(key, value) for key, value in request.scope["headers"] if key != b"content-type"]
    headers.append((b"content-type", b"application/json"))
    proxy = Request({**request.scope, "headers": headers}, request.receive)
    proxy._body = json.dumps(data, ensure_ascii=False).encode()
    return proxy


def _numbered(errors: list[dict]) -> list[dict]:
    """Add the 1-based record number to schema errors inside ``records``."""
    numbered = []
    for error in errors:
        loc = tuple(error.get("loc", ()))
        if len(loc) >= 3 and loc[:2] == ("body", "records") and isinstance(loc[2], int):
            error = {**error, "record": loc[2] + 1}
        numbered.append(error)
    return numbered


class BatchRoute(APIRoute):
    """POST: media type, bounded body read before parsing, XML, 413 for the record count.

    The exact bytes are kept on ``request.state`` for hashing and storage; FastAPI then
    parses the cached body, so the OpenAPI schema stays the plain ``ObservationBatch``.
    An XML body is converted to the same dict first (``batch_from_xml``) and validated
    by the same model; its structure errors are 422 in the same error format.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()
        if "POST" not in self.methods:
            return handler

        async def bounded(request: Request) -> Response:
            container = _body_container(request.headers.get("content-type"))
            if container is None:
                return JSONResponse(status_code=415, content={"detail": "unsupported_media_type"})
            declared = request.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_OBSERVATION_BYTES:
                return _too_large("batch_too_large")
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_OBSERVATION_BYTES:
                    return _too_large("batch_too_large")
                chunks.append(chunk)
            body = b"".join(chunks)
            request._body = body  # Starlette's body cache, read by FastAPI below
            request.state.batch_bytes = body
            request.state.batch_container = container
            try:
                if container == "xml":
                    try:
                        # Off the event loop: up to 5 MiB of XML takes a fraction of a second.
                        data = await run_in_threadpool(batch_from_xml, body)
                    except BatchXmlError as error:
                        raise RequestValidationError(error.errors, body=None) from error
                    request = _as_json(request, data)
                return await handler(request)
            except RequestValidationError as error:
                errors = list(error.errors())
                if any(
                    tuple(item.get("loc", ())) == ("body", "records")
                    and item.get("type") == "too_long"
                    for item in errors
                ):
                    return _too_large("too_many_records")
                raise RequestValidationError(_numbered(errors), body=error.body) from error

        return bounded


def build_router(config: Settings) -> APIRouter:
    router = APIRouter(route_class=BatchRoute)
    fixture = config.mode == "fixture"

    def storage_dsn() -> str:
        if config.db_dsn is None:
            raise HTTPException(status_code=503, detail="imports_not_configured")
        return config.db_dsn.get_secret_value()

    def within_rate(
        request: Request, actor: Annotated[Me, Depends(require_permission("ingest"))]
    ) -> Me:
        """Per token (or administrator) one-minute window, before the body is validated."""
        limiter = auth_runtime(request).token_limiter
        identity = current_integration(request)
        key = identity.token_id if identity is not None else f"user:{actor.subject_id}"
        wait = limiter.acquire(key) if limiter is not None else 0
        if wait:
            raise HTTPException(
                status_code=429, detail="rate_limited", headers={"Retry-After": str(wait)}
            )
        return actor

    def replay(dsn: str, stored: StoredBatch, sha256: str, response: Response) -> ImportFile:
        """The same key again: same bytes -> 200 with the stored report, else 409."""
        if stored.sha256 != sha256:
            raise HTTPException(status_code=409, detail="batch_id_conflict")
        found = read_import(dsn, stored.import_id)
        if found is None:  # pragma: no cover - the register references import_files
            raise HTTPException(status_code=503, detail=STORAGE_DOWN)
        response.status_code = 200
        return found

    @router.post(
        "/api/v1/observations",
        response_model=ImportFile,
        status_code=202,
        openapi_extra=BATCH_BODY,
        responses={415: {"description": "Content-Type is not JSON or XML"}},
    )
    def ingest(
        request: Request,
        response: Response,
        batch: ObservationBatch,
        actor: Annotated[Me, Depends(within_rate)],
    ) -> ImportFile:
        if fixture:
            canonical = json.dumps(
                batch.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
            ).encode()
            return ImportFile(
                id=f"synthetic-batch-{hashlib.sha256(canonical).hexdigest()[:12]}",
                format="journal_json",
                file_name=batch.batch_id,
                sha256=hashlib.sha256(canonical).hexdigest(),
                size_bytes=len(canonical),
                uploaded_by=actor.subject_id,
                uploaded_at=forecast_fixture.FIXTURE_AS_OF,
                status="queued",
                simulated=True,
            )
        dsn = storage_dsn()
        raw: bytes = request.state.batch_bytes
        suffix = request.state.batch_container  # json or xml: the body is kept as sent
        sha256 = hashlib.sha256(raw).hexdigest()
        client_id = actor.subject_id
        identity = current_integration(request)
        token_id = identity.token_id if identity is not None else None
        try:
            stored = find_batch(dsn, client_id=client_id, batch_id=batch.batch_id)
            if stored is not None:
                return replay(dsn, stored, sha256, response)
        except psycopg.Error as error:
            raise HTTPException(status_code=503, detail=STORAGE_DOWN) from error

        import_id = new_import_id()
        stored_name = f"{import_id}.{suffix}"
        started = perf_counter()
        try:
            store_upload(io.BytesIO(raw), config.upload_dir, stored_name, MAX_OBSERVATION_BYTES)
        except OSError as error:
            raise HTTPException(status_code=503, detail="upload_storage_unavailable") from error
        audit = AuditEvent(
            action="observations.received",
            actor_id=client_id,
            actor_role=acting_role(actor, "ingest"),
            target_kind="import_file",
            target_id=import_id,
            request_id=request_id(request),
            client_address=client_address(request),
            details={
                "batch_id": batch.batch_id,
                "records": len(batch.records),
                "sha256": sha256,
                "size_bytes": len(raw),
                "container": suffix,
                "token_id": token_id,
            },
        )
        try:
            row = register_batch(
                dsn,
                client_id=client_id,
                batch_id=batch.batch_id,
                token_id=token_id,
                records=len(batch.records),
                import_id=import_id,
                file_name=f"observations-{batch.batch_id}.{suffix}"[:255],
                stored_name=stored_name,
                sha256=sha256,
                size_bytes=len(raw),
                store_seconds=perf_counter() - started,
                audit=audit,
            )
        except BatchRaced:
            # A concurrent request with the same key committed first.
            stored_path(config.upload_dir, stored_name).unlink(missing_ok=True)
            try:
                stored = find_batch(dsn, client_id=client_id, batch_id=batch.batch_id)
            except psycopg.Error as error:
                raise HTTPException(status_code=503, detail=STORAGE_DOWN) from error
            if stored is None:  # pragma: no cover - the winner has committed
                raise HTTPException(status_code=503, detail=STORAGE_DOWN) from None
            return replay(dsn, stored, sha256, response)
        except psycopg.Error as error:
            stored_path(config.upload_dir, stored_name).unlink(missing_ok=True)
            raise HTTPException(status_code=503, detail=STORAGE_DOWN) from error
        return to_model(row)

    @router.get("/api/v1/observations/{import_id}", response_model=ImportFile)
    def batch_status(
        import_id: str, actor: Annotated[Me, Depends(require_permission("ingest"))]
    ) -> ImportFile:
        if fixture:
            found = product_fixture.observation_batch(import_id)
        else:
            dsn = storage_dsn()
            try:
                owner = batch_owner(dsn, import_id)
                # An integration sees only its own batches; an administrator sees all.
                visible = owner is not None and (
                    owner == actor.subject_id or "admin" in actor.roles
                )
                found = read_import(dsn, import_id) if visible else None
            except psycopg.Error as error:
                raise HTTPException(status_code=503, detail=STORAGE_DOWN) from error
        if found is None:
            raise HTTPException(status_code=404, detail="batch_not_found")
        return found

    return router

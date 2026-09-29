"""API documentation: Swagger UI at ``/api/docs`` and the schema at ``/api/openapi.json``.

Served by the API itself when ``INFRA_PUBLIC_DOCS=true`` (always in ``dev_stub``); only
the documentation is public: every data route keeps its session or token check
(``backend/tests/test_rbac_matrix.py``). The root ``/docs``, ``/redoc`` and
``/openapi.json`` are not served, and Caddy answers 404 there (``deploy/Caddyfile``).
ReDoc is not served: its standalone build needs ``blob:`` workers the site CSP forbids.

No CDN and no inline script: the Swagger UI files come from the pinned
``fastapi-swagger`` wheel, whose resources are the files of swagger-ui-dist 5.33.0 byte
for byte (Apache-2.0; hashes checked in ``backend/tests/test_api_docs.py``; the license
notice the bundle refers to is served next to it), and the page starts Swagger UI from
``/api/docs/swagger-init.js``. The page therefore runs under the
stand's site-wide CSP (``script-src 'self'``) without an exception; the API sends the
same policy itself for access without Caddy (SSH tunnel, local run).

The schema names three security schemes, so «Authorize» in Swagger UI works:

* ``integrationToken`` — ``Authorization: Bearer ipk_...`` (``auth/tokens.py``), the
  permission ``ingest`` only: ``POST /api/v1/observations`` and batch status;
* ``sessionCookie`` — ``__Host-infrapulse-session`` from the login of the web interface
  of the same site. A browser page cannot set it; a request from the same site sends it
  by itself, so GET routes work after signing in to the interface;
* ``csrfToken`` — ``X-CSRF-Token`` with the value of the ``__Host-infrapulse-csrf``
  cookie, required with the session for POST.

Each operation lists the schemes that open it (``security``), derived from its
``require_permission`` dependency; open routes (``/health/*``, login, logout) list none.
Swagger UI keeps entered credentials only in page memory (``persistAuthorization`` off).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from importlib import resources
from typing import Any

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.responses import HTMLResponse, Response
from fastapi.routing import APIRoute, iter_route_contexts
from starlette.routing import BaseRoute

from infra_pulse_backend.api.auth_deps import UNSAFE_METHODS, current_actor
from infra_pulse_backend.auth.sessions import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE

DOCS_URL = "/api/docs"
OPENAPI_URL = "/api/openapi.json"
# Swagger UI resources of the fastapi-swagger wheel: published name -> media type.
ASSETS = {
    "swagger-ui-bundle.js": "text/javascript; charset=utf-8",
    "swagger-ui.css": "text/css; charset=utf-8",
    "favicon-32x32.png": "image/png",
}
INIT_SCRIPT = "swagger-init.js"
# The first line of swagger-ui-bundle.js points to this file; the wheel does not ship it.
LICENSE_NOTICE = "swagger-ui-bundle.js.LICENSE.txt"
# Every path the documentation adds (the test checks nothing else became anonymous).
DOCS_PATHS = frozenset(
    {DOCS_URL, f"{DOCS_URL}/", OPENAPI_URL, f"{DOCS_URL}/{INIT_SCRIPT}"}
    | {f"{DOCS_URL}/{name}" for name in (*ASSETS, LICENSE_NOTICE)}
)
LICENSE_TEXT = """Swagger UI 5.33.0 (npm package swagger-ui-dist), https://github.com/swagger-api/swagger-ui
Licensed under the Apache License, Version 2.0: https://www.apache.org/licenses/LICENSE-2.0

swagger-ui-bundle.js, swagger-ui.css and favicon-32x32.png are served unchanged, as shipped
in the fastapi-swagger 0.4.60 wheel (MIT). The license comments of the third-party code
bundled in swagger-ui-bundle.js are in the file swagger-ui-bundle.js.LICENSE.txt of the
swagger-ui-dist 5.33.0 package.
"""
# The site-wide policy of deploy/Caddyfile, sent by the API too (test: the two are equal).
DOCS_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)
DESCRIPTION = (
    "API InfraPulse MSK. Документация открыта для чтения; каждый маршрут, кроме "
    "`/health/*` и входа, проверяет сессию или токен интеграции.\n\n"
    "* **Чтение (GET)** — по cookie сессии: войти в веб-интерфейс этого же сайта, "
    "затем «Try it out» здесь.\n"
    "* **Пачки наблюдений** `POST /api/v1/observations` — токен интеграции: "
    "«Authorize» → `integrationToken`, значение `ipk_...` без слова Bearer.\n"
    "* **Запись под сессией (POST)** — дополнительно `csrfToken`: значение cookie "
    f"`{CSRF_COOKIE}`.\n\n"
    "Инструкция для систем-источников — `docs/INTEGRATION_API.md` в репозитории."
)
SECURITY_SCHEMES: dict[str, dict[str, str]] = {
    "integrationToken": {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "ipk_...",
        "description": (
            "Токен интеграции внешней системы (выдаёт администратор сервиса). "
            "Даёт только право ingest: отправку пачек и статус своих пачек."
        ),
    },
    "sessionCookie": {
        "type": "apiKey",
        "in": "cookie",
        "name": SESSION_COOKIE,
        "description": (
            "Сессия входа через каталог. Ставится при входе в веб-интерфейс этого же "
            "сайта и отправляется браузером сама; поле в «Authorize» заполнять не нужно."
        ),
    },
    "csrfToken": {
        "type": "apiKey",
        "in": "header",
        "name": CSRF_HEADER,
        "description": (
            f"Для POST под сессией: значение cookie {CSRF_COOKIE}. С токеном интеграции не нужен."
        ),
    },
}

PAGE = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>InfraPulse MSK — API</title>
<link rel="icon" type="image/png" href="{DOCS_URL}/favicon-32x32.png">
<link rel="stylesheet" href="{DOCS_URL}/swagger-ui.css">
</head>
<body>
<div id="swagger-ui"></div>
<script src="{DOCS_URL}/swagger-ui-bundle.js"></script>
<script src="{DOCS_URL}/{INIT_SCRIPT}"></script>
</body>
</html>
"""
# External start script: the CSP allows no inline script. No validator badge (it would
# call validator.swagger.io), no configuration from the query string, credentials are
# not written to localStorage.
INIT_JS = f"""window.addEventListener("load", function () {{
  window.ui = SwaggerUIBundle({{
    url: "{OPENAPI_URL}",
    dom_id: "#swagger-ui",
    layout: "BaseLayout",
    deepLinking: true,
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
    validatorUrl: null,
    queryConfigEnabled: false,
    persistAuthorization: false,
    displayRequestDuration: true
  }});
}});
"""
COMMON_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
PAGE_HEADERS = COMMON_HEADERS | {"Content-Security-Policy": DOCS_CSP, "Cache-Control": "no-cache"}
ASSET_HEADERS = COMMON_HEADERS | {"Cache-Control": "public, max-age=3600"}


def swagger_ui_file(name: str) -> bytes:
    return resources.files("fastapi_swagger.resources").joinpath(name).read_bytes()


def route_auth(dependant: Dependant) -> str | None:
    """The permission a route requires, ``"session"`` for any signed-in caller, or None."""
    session = None
    for dependency in dependant.dependencies:
        permission = getattr(dependency.call, "permission", None)
        if permission is not None:
            return permission
        found = route_auth(dependency)
        if found not in (None, "session"):
            return found
        if dependency.call is current_actor or found == "session":
            session = "session"
    return session


def operation_security(auth: str, method: str) -> list[dict[str, list[str]]]:
    session: dict[str, list[str]] = {"sessionCookie": []}
    if method in UNSAFE_METHODS:
        session["csrfToken"] = []
    if auth == "ingest":
        return [{"integrationToken": []}, session]
    return [session]


def add_security(schema: dict[str, Any], routes: Sequence[BaseRoute]) -> None:
    schemes = {name: dict(scheme) for name, scheme in SECURITY_SCHEMES.items()}
    schema.setdefault("components", {})["securitySchemes"] = schemes
    # Included routers are wrappers in app.routes; iterate the effective routes.
    for context in iter_route_contexts(routes):
        if not isinstance(context.route, APIRoute) or not context.include_in_schema:
            continue
        auth = route_auth(context.dependant)
        if auth is None:
            continue
        for method in context.methods or ():
            operation = schema["paths"].get(context.path_format, {}).get(method.lower())
            if operation is not None:
                operation["security"] = operation_security(auth, method)


def with_security(app: FastAPI) -> Callable[[], dict[str, Any]]:
    """``app.openapi`` that adds the security schemes to FastAPI's cached schema."""
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        schema = generate()
        if "securitySchemes" not in schema.get("components", {}):
            add_security(schema, app.routes)
        return schema

    return openapi


def install(app: FastAPI, *, serve: bool) -> None:
    """Security schemes in the schema always; the Swagger UI routes when ``serve``.

    The schema route itself is FastAPI's (``openapi_url``); these routes are hidden
    from the schema and need no credentials.
    """
    app.openapi = with_security(app)  # type: ignore[method-assign]
    if not serve:
        return

    def page() -> HTMLResponse:
        return HTMLResponse(PAGE, headers=PAGE_HEADERS)

    app.add_api_route(DOCS_URL, page, include_in_schema=False, response_class=HTMLResponse)
    # Not a redirect: behind nginx the API sees Host api:8000 and would redirect there.
    app.add_api_route(f"{DOCS_URL}/", page, include_in_schema=False, response_class=HTMLResponse)

    files = {name: swagger_ui_file(name) for name in ASSETS}
    files[INIT_SCRIPT] = INIT_JS.encode()
    files[LICENSE_NOTICE] = LICENSE_TEXT.encode()
    media_types = ASSETS | {
        INIT_SCRIPT: ASSETS["swagger-ui-bundle.js"],
        LICENSE_NOTICE: "text/plain; charset=utf-8",
    }
    for name, body in files.items():
        media_type = media_types[name]
        app.add_api_route(
            f"{DOCS_URL}/{name}", static_file(body, media_type), include_in_schema=False
        )


def static_file(body: bytes, media_type: str) -> Callable[[], Response]:
    """An endpoint without parameters (FastAPI would read any as query parameters)."""

    def endpoint() -> Response:
        return Response(body, media_type=media_type, headers=ASSET_HEADERS)

    return endpoint

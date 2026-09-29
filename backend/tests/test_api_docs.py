"""Public API documentation /api/docs (Swagger UI) and /api/openapi.json.

The page must run under the stand's CSP without an exception: no inline script, every
file from the API itself, Swagger UI files equal to the official swagger-ui-dist. The
schema must carry no host, address or secret, and show both bodies of the batch API.
Anonymous access to data routes is checked in ``test_rbac_matrix.py``.
"""

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.api.docs import ASSETS, DOCS_CSP, DOCS_URL, INIT_SCRIPT, OPENAPI_URL
from infra_pulse_backend.api.observations import JSON_EXAMPLE, XML_EXAMPLE
from infra_pulse_backend.config import Settings
from infra_pulse_backend.ingestion.batch_xml import batch_from_xml
from infra_pulse_core.contracts.imports import ObservationBatch

ROOT = Path(__file__).resolve().parents[2]
# swagger-ui-dist 5.33.0 from registry.npmjs.org (sha512-wpdK+m6BU5yj...I7A==, Apache-2.0).
# fastapi-swagger 0.4.60 ships these files unchanged. On an upgrade compare the new wheel
# with the npm package of the same Swagger UI version and update the hashes.
SWAGGER_UI_DIST = {
    "swagger-ui-bundle.js": "62df541529080464a7660adc793eab7128c6193ce3be24ddc1e0e0a4a63edc2f",
    "swagger-ui.css": "1ac324f7dcd27e4b9386b4bd6421271ec147e922a22c05ba24b11515e9aa6321",
    "favicon-32x32.png": "3ed612f41e050ca5e7000cad6f1cbe7e7da39f65fca99c02e99e6591056e5837",
}


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.scripts: list[dict[str, str | None]] = []
        self.inline: list[str] = []
        self.handlers: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.handlers += [name for name in values if name.startswith("on")]
        self.urls += [values[name] for name in ("src", "href") if values.get(name)]
        if tag == "script":
            self.scripts.append(values)
            self._in_script = True
        if tag == "style":
            self.inline.append("<style>")

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script and data.strip():
            self.inline.append(data)


def public_client() -> TestClient:
    app = create_app(Settings(mode="fixture", auth_mode="ldap", public_docs=True, _env_file=None))
    return TestClient(app, base_url="https://testserver")


def test_page_uses_only_own_files_and_no_inline_script():
    client = public_client()
    response = client.get(DOCS_URL)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-security-policy"] == DOCS_CSP
    assert response.headers["x-content-type-options"] == "nosniff"
    page = Page()
    page.feed(response.text)
    assert page.inline == [] and page.handlers == []
    assert [script["src"] for script in page.scripts] == [
        f"{DOCS_URL}/swagger-ui-bundle.js",
        f"{DOCS_URL}/{INIT_SCRIPT}",
    ]
    assert page.urls and all(url.startswith(f"{DOCS_URL}/") for url in page.urls)
    assert "//" not in response.text.replace("<!doctype html>", "")
    assert client.get(f"{DOCS_URL}/").text == response.text
    for url in page.urls:
        assert client.get(url).status_code == 200, url


def test_init_script_reads_the_own_schema_without_external_calls():
    response = public_client().get(f"{DOCS_URL}/{INIT_SCRIPT}")
    assert response.headers["content-type"].startswith("text/javascript")
    script = response.text
    assert f'url: "{OPENAPI_URL}"' in script
    # No validator badge (validator.swagger.io), no ?url= configuration, no localStorage.
    for setting in ("validatorUrl: null", "queryConfigEnabled: false"):
        assert setting in script
    assert "persistAuthorization: false" in script
    assert "http" not in script and "//" not in script


@pytest.mark.parametrize("name", sorted(ASSETS))
def test_swagger_ui_files_are_the_official_distribution(name):
    response = public_client().get(f"{DOCS_URL}/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"] == ASSETS[name]
    assert hashlib.sha256(response.content).hexdigest() == SWAGGER_UI_DIST[name]


def test_license_notice_the_bundle_refers_to_is_served():
    client = public_client()
    first_line = client.get(f"{DOCS_URL}/swagger-ui-bundle.js").text.split("\n", 1)[0]
    reference = re.fullmatch(r"/\*! For license information please see (\S+) \*/", first_line)
    assert reference is not None, first_line
    notice = client.get(f"{DOCS_URL}/{reference.group(1)}")
    assert notice.status_code == 200
    assert notice.headers["content-type"] == "text/plain; charset=utf-8"
    assert "Swagger UI 5.33.0" in notice.text
    assert "Apache License, Version 2.0" in notice.text


def test_site_csp_is_the_policy_checked_with_the_docs():
    """deploy/Caddyfile keeps one site-wide CSP, equal to the docs one; no /api exception."""
    caddyfile = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
    policies = re.findall(r'Content-Security-Policy "([^"]*)"', caddyfile)
    assert policies[0] == DOCS_CSP
    exceptions = re.findall(r"^\s*header (\S+) \{", caddyfile, flags=re.MULTILINE)
    assert exceptions == ["/grafana/*"]
    closed = re.search(r"@apidocs path ([^\n]+)", caddyfile)
    assert closed is not None
    assert set(closed.group(1).split()) >= {"/docs", "/redoc", "/openapi.json"}
    assert not any(path.startswith("/api") for path in closed.group(1).split())


def test_schema_has_no_host_address_or_secret():
    schema = public_client().get(OPENAPI_URL).json()
    assert "servers" not in schema
    text = json.dumps(schema, ensure_ascii=False)
    forbidden = [
        r"https?://",
        r"ldaps?://",
        r"postgres(ql)?://",
        r"\bipk_[A-Za-z0-9_-]{8,}",
        r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
        r"localhost",
        r"infra-pulse\.ru",
        r"(?i)password\s*[=:]\s*\S",
    ]
    for pattern in forbidden:
        assert re.search(pattern, text) is None, pattern


def test_batch_api_shows_json_and_xml_examples_of_one_batch():
    schema = public_client().get(OPENAPI_URL).json()
    content = schema["paths"]["/api/v1/observations"]["post"]["requestBody"]["content"]
    assert set(content) == {"application/json", "application/xml"}
    for body in content.values():
        assert body["schema"] == {"$ref": "#/components/schemas/ObservationBatch"}
    assert content["application/json"]["example"] == JSON_EXAMPLE
    assert content["application/xml"]["example"] == XML_EXAMPLE
    from_json = ObservationBatch.model_validate(JSON_EXAMPLE)
    from_xml = ObservationBatch.model_validate(batch_from_xml(XML_EXAMPLE.encode()))
    assert from_json == from_xml
    assert from_json.batch_id.startswith("scada-ods-1-")

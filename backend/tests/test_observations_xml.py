"""XML observation batches without PostgreSQL: converter, safe parsing, HTTP in fixture mode.

The PostgreSQL path (storage as sent, worker, idempotency, audit) is in
``test_integration_db.py``. All records are synthetic.
"""

import json

import pytest
from fastapi.testclient import TestClient

from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.api.observations import XML_EXAMPLE
from infra_pulse_backend.config import Settings
from infra_pulse_backend.ingestion.batch_xml import MAX_ERRORS, BatchXmlError, batch_from_xml
from infra_pulse_backend.ingestion.csv_source import FileRejected
from infra_pulse_backend.ingestion.journal_csv import dedup_identity
from infra_pulse_backend.ingestion.journal_json import parse_batch
from infra_pulse_core.contracts.imports import MAX_OBSERVATION_RECORDS, ObservationBatch

XML = {"Content-Type": "application/xml"}
RECORDS = [
    {
        "event_id": "9000001",
        "channel_id": "700001",
        "date": "2026-09-20",
        "time": "03:09:27",
        "alarm": False,
        "value": "28",
    },
    {
        "ид_события": "9000002",
        "ид_канала_данных": "700002",
        "дата": "2026-09-20",
        "время": "10:00:00",
        "тревожное": True,
        "значение_датчика": "Неисправен",
    },
    {
        "event_id": "9000003",
        "channel_id": "700003",
        "event_at": "2026-09-20T08:00:00Z",
        "alarm": False,
        "value": "1,5",
    },
]


def xml_text(value: object) -> str:
    text = str(value).lower() if isinstance(value, bool) else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def xml_batch(records: list[dict], batch_id: str = "b-0001", *, as_element=False) -> bytes:
    """The same batch in XML: ``batch_id`` as an attribute (or element), fields as elements."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append("<batch>" if as_element else f'<batch batch_id="{batch_id}">')
    if as_element:
        lines.append(f"  <batch_id>{batch_id}</batch_id>")
    for record in records:
        fields = "".join(f"<{name}>{xml_text(value)}</{name}>" for name, value in record.items())
        lines.append(f"  <record>{fields}</record>")
    lines.append("</batch>")
    return ("\n".join(lines) + "\n").encode()


def json_batch(records: list[dict], batch_id: str = "b-0001") -> bytes:
    return json.dumps({"batch_id": batch_id, "records": records}, ensure_ascii=False).encode()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings(mode="fixture", _env_file=None)))


def test_xml_is_the_same_batch_as_json():
    for as_element in (False, True):
        converted = batch_from_xml(xml_batch(RECORDS, as_element=as_element))
        assert converted == {"batch_id": "b-0001", "records": RECORDS}
        assert ObservationBatch.model_validate(converted) == ObservationBatch.model_validate_json(
            json_batch(RECORDS)
        )
    # The worker reads both into the same journal rows (the CSV overlap key included).
    from_xml, from_json = parse_batch(xml_batch(RECORDS), "xml"), parse_batch(json_batch(RECORDS))
    assert (from_xml.container, from_json.container) == ("xml", "json")
    assert from_xml.rows == from_json.rows and from_xml.quarantined == []
    assert [dedup_identity(row) for row in from_xml.rows] == [
        dedup_identity(row) for row in from_json.rows
    ]
    # The OpenAPI example is a valid batch.
    assert len(ObservationBatch.model_validate(batch_from_xml(XML_EXAMPLE.encode())).records) == 3


def test_text_is_verbatim_and_alarm_is_exactly_true_or_false():
    data = (
        b'<batch batch_id="b"><record><channel_id>7</channel_id><alarm>t</alarm>'
        b"<value>  28 </value><event_id/><date><![CDATA[2026-09-20]]></date>"
        b"<!-- a comment is ignored --><time>03:09:27</time></record>"
        b"<record><alarm>TRUE</alarm><value>a &amp; b &lt;c&gt;</value></record>"
        b"<record><alarm>true</alarm><value/></record></batch>"
    )
    first, second, third = batch_from_xml(data)["records"]
    assert first == {
        "channel_id": "7",
        "alarm": "t",
        "value": "  28 ",
        "event_id": "",
        "date": "2026-09-20",
        "time": "03:09:27",
    }
    assert (second["alarm"], second["value"]) == ("TRUE", "a & b <c>")
    assert (third["alarm"], third["value"]) == (True, "")
    # windows-1251 named in the XML declaration is decoded by the XML rule.
    cp1251 = '<?xml version="1.0" encoding="windows-1251"?><batch batch_id="b"><record>'
    cp1251 += "<value>Неисправен</value></record></batch>"
    assert batch_from_xml(cp1251.encode("cp1251"))["records"] == [{"value": "Неисправен"}]


LOL = "".join(f'<!ENTITY lol{n} "{f"&lol{n - 1};" * 10}">' for n in range(1, 10))
DANGEROUS = {
    "billion_laughs": f'<?xml version="1.0"?><!DOCTYPE batch [<!ENTITY lol0 "lol">{LOL}]>'
    '<batch batch_id="b"><record><value>&lol9;</value></record></batch>',
    "external_entity": '<?xml version="1.0"?><!DOCTYPE batch [<!ENTITY xxe SYSTEM '
    '"file:///etc/passwd">]><batch batch_id="b"><record><value>&xxe;</value></record></batch>',
    "external_dtd": '<?xml version="1.0"?><!DOCTYPE batch SYSTEM "http://127.0.0.1:9/b.dtd">'
    '<batch batch_id="b"/>',
    "parameter_entity": '<?xml version="1.0"?><!DOCTYPE batch [<!ENTITY % p SYSTEM '
    '"http://127.0.0.1:9/p.dtd"> %p;]><batch batch_id="b"/>',
    "plain_doctype": '<!DOCTYPE batch><batch batch_id="b"/>',
}


@pytest.mark.parametrize("name", sorted(DANGEROUS))
def test_dtd_and_entities_are_refused(name):
    with pytest.raises(BatchXmlError) as error:
        batch_from_xml(DANGEROUS[name].encode())
    assert [item["type"] for item in error.value.errors] == ["xml_forbidden"]
    # The worker refuses the same bytes (never reached: the API refuses them first).
    with pytest.raises(FileRejected):
        parse_batch(DANGEROUS[name].encode(), "xml")


@pytest.mark.parametrize(
    ("data", "kind", "loc"),
    [
        (b"<batch", "xml_invalid", ["body"]),
        (b'<batch batch_id="b"><record><value>&undefined;</value></record></batch>', "xml_invalid",
         ["body"]),
        (b'<batch batch_id="\xff"/>', "xml_invalid", ["body"]),
        (b'<records batch_id="b"/>', "xml_structure", ["body"]),
        (b'<batch xmlns="urn:x" batch_id="b"/>', "xml_structure", ["body"]),
        (b'<batch batch_id="b"><records><record/></records></batch>', "xml_structure",
         ["body", "records"]),
        (b'<batch batch_id="b"><batch_id>b</batch_id></batch>', "xml_duplicate_field",
         ["body", "batch_id"]),
        (b'<batch batch_id="b">text<record/></batch>', "xml_structure", ["body"]),
        (b'<batch batch_id="b"><record id="1"/></batch>', "xml_structure", ["body", "records", 0]),
        (b'<batch batch_id="b"><record><value>1</value><value>2</value></record></batch>',
         "xml_duplicate_field", ["body", "records", 0, "value"]),
        (b'<batch batch_id="b"><record><value><b>1</b></value></record></batch>',
         "xml_structure", ["body", "records", 0, "value"]),
        (b'<batch batch_id="b"><record><value unit="V">1</value></record></batch>',
         "xml_structure", ["body", "records", 0, "value"]),
    ],
)  # fmt: skip
def test_structure_errors_name_the_place(data, kind, loc):
    with pytest.raises(BatchXmlError) as error:
        batch_from_xml(data)
    assert [(item["type"], list(item["loc"])) for item in error.value.errors][:1] == [(kind, loc)]


def test_hostile_sizes_are_cut_short():
    # A million repeated fields: at most MAX_ERRORS errors, not a million-line answer.
    repeated = b'<batch batch_id="b"><record>' + b"<a/>" * 1_000_000 + b"</record></batch>"
    with pytest.raises(BatchXmlError) as error:
        batch_from_xml(repeated)
    assert len(error.value.errors) == MAX_ERRORS
    # More records than the contract allows: the contract's too_long, before reading them.
    many = b'<batch batch_id="b">' + b"<record/>" * (MAX_OBSERVATION_RECORDS + 1) + b"</batch>"
    with pytest.raises(BatchXmlError) as error:
        batch_from_xml(many)
    assert [(item["type"], item["loc"]) for item in error.value.errors] == [
        ("too_long", ("body", "records"))
    ]


def test_http_accepts_xml_with_the_same_checks_as_json(client):
    for content_type in ("application/xml", "text/xml; charset=utf-8"):
        response = client.post(
            "/api/v1/observations",
            content=xml_batch(RECORDS),
            headers={"Content-Type": content_type},
        )
        assert response.status_code == 202, response.text
        assert response.json()["simulated"] is True
    # Fixture mode stores nothing; its report is derived from the validated batch.
    as_json = client.post(
        "/api/v1/observations",
        content=json_batch(RECORDS),
        headers={"Content-Type": "application/json"},
    )
    assert as_json.json()["sha256"] == response.json()["sha256"]

    extra = [RECORDS[0], {**RECORDS[1], "лишнее": "1"}, {**RECORDS[2], "alarm": "t"}]
    response = client.post("/api/v1/observations", content=xml_batch(extra), headers=XML)
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert sorted((error["type"], error["record"]) for error in errors) == [
        ("bool_type", 3),
        ("extra_forbidden", 2),
    ]
    extra_batch_field = xml_batch(RECORDS).replace(b"<batch ", b'<batch source="scada" ')
    response = client.post("/api/v1/observations", content=extra_batch_field, headers=XML)
    assert [(e["type"], e["loc"]) for e in response.json()["detail"]] == [
        ("extra_forbidden", ["body", "source"])
    ]

    empty = client.post("/api/v1/observations", content=xml_batch([]), headers=XML)
    assert (empty.status_code, empty.json()["detail"][0]["type"]) == (422, "too_short")
    too_many = xml_batch([RECORDS[0]] * (MAX_OBSERVATION_RECORDS + 1))
    response = client.post("/api/v1/observations", content=too_many, headers=XML)
    assert (response.status_code, response.json()["detail"]) == (413, "too_many_records")
    for batch_id in ("два слова", "x" * 129):
        response = client.post(
            "/api/v1/observations", content=xml_batch(RECORDS, batch_id), headers=XML
        )
        assert response.status_code == 422, batch_id
        assert response.json()["detail"][0]["loc"] == ["body", "batch_id"]
    long_value = xml_batch([{**RECORDS[0], "value": "x" * 1001}])
    response = client.post("/api/v1/observations", content=long_value, headers=XML)
    assert (response.status_code, response.json()["detail"][0]["type"]) == (422, "string_too_long")
    huge = b'<batch batch_id="b">' + b" " * (5 * 1024 * 1024) + b"</batch>"
    response = client.post("/api/v1/observations", content=huge, headers=XML)
    assert (response.status_code, response.json()["detail"]) == (413, "batch_too_large")


@pytest.mark.parametrize("name", sorted(DANGEROUS))
def test_http_refuses_dtd_and_entities(client, name):
    response = client.post("/api/v1/observations", content=DANGEROUS[name].encode(), headers=XML)
    assert response.status_code == 422
    assert [error["type"] for error in response.json()["detail"]] == ["xml_forbidden"]


@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "application/x-www-form-urlencoded", "text/csv", "application/xhtml+xml"],
)
def test_other_media_types_are_415(client, content_type):
    response = client.post(
        "/api/v1/observations", content=json_batch(RECORDS), headers={"Content-Type": content_type}
    )
    assert (response.status_code, response.json()["detail"]) == (415, "unsupported_media_type")


def test_json_media_types_stay_json_and_no_header_is_415(client):
    for content_type in ("application/json; charset=utf-8", "application/vnd.batch+json"):
        response = client.post(
            "/api/v1/observations",
            content=json_batch(RECORDS),
            headers={"Content-Type": content_type},
        )
        assert response.status_code == 202, response.text
    response = client.post("/api/v1/observations", content=json_batch(RECORDS))
    assert (response.status_code, response.json()["detail"]) == (415, "unsupported_media_type")
    # JSON sent as XML is not XML.
    response = client.post("/api/v1/observations", content=json_batch(RECORDS), headers=XML)
    assert (response.status_code, response.json()["detail"][0]["type"]) == (422, "xml_invalid")


def test_openapi_documents_the_xml_body():
    app = create_app(Settings(mode="scaffold", _env_file=None))
    operation = app.openapi()["paths"]["/api/v1/observations"]["post"]
    content = operation["requestBody"]["content"]
    assert set(content) == {"application/json", "application/xml"}
    assert content["application/xml"]["schema"] == content["application/json"]["schema"]
    assert "<batch batch_id=" in content["application/xml"]["example"]
    assert "415" in operation["responses"]

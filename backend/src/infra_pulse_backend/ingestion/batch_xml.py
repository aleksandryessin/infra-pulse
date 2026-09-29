"""XML form of an observation batch (``POST /api/v1/observations``, ТЗ §7).

The batch is the same ``ObservationBatch`` as the JSON body; XML only spells it:

.. code-block:: xml

    <batch batch_id="scada-ods-1-20260630T120000-000184">
      <record>
        <event_id>9000001</event_id>
        <channel_id>demo-700001</channel_id>
        <date>2026-06-30</date>
        <time>11:59:58</time>
        <alarm>false</alarm>
        <value>28</value>
      </record>
    </batch>

* the root is ``<batch>`` without a namespace; ``batch_id`` is its attribute or a
  ``<batch_id>`` child element, not both;
* every ``<record>`` is a direct child of ``<batch>`` (no ``<records>`` wrapper); its
  fields are child elements named like the JSON keys, in English or as in the CSV
  header (``<ид_события>``, ``<значение_датчика>`` ...), in any order, each at most once;
* a field element holds text only: no attributes, no child elements. The text is
  taken verbatim, whitespace included (``value`` is the source text); an empty element
  is an empty string. ``alarm`` is ``true`` or ``false`` exactly; any other text is
  left as text, so the contract rejects it as it rejects ``"t"`` in JSON;
* text outside the field elements (other than whitespace), attributes on ``<record>``
  and repeated fields are structure errors. Unknown fields and attributes of
  ``<batch>`` reach the contract, which forbids extra fields (``extra_forbidden``).

The result is a plain dict validated by the contract, so XML and JSON batches share
every check, limit and error location (``["body", "records", i, field]``).

Parsing is ``defusedxml`` with ``forbid_dtd``: any ``<!DOCTYPE>`` (hence every entity
declaration, internal or external, and the «billion laughs» expansion) is refused
before it is processed, and no external resource is ever fetched. Comments and
processing instructions are ignored. The encoding is the XML one: UTF-8 unless the
XML declaration names another; a ``charset`` in ``Content-Type`` is not used.
"""

from __future__ import annotations

from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from infra_pulse_core.contracts.imports import MAX_OBSERVATION_RECORDS

ROOT = "batch"
RECORD = "record"
RECORDS = "records"
ALARM_FIELDS = ("alarm", "тревожное")
ALARM_TEXT = {"true": True, "false": False}
# Structure errors reported at most; the conversion stops there (a hostile body with a
# million repeated elements must not produce a million-line answer).
MAX_ERRORS = 100


class BatchXmlError(ValueError):
    """The body is not an XML batch; ``errors`` are in the pydantic error format."""

    def __init__(self, errors: list[dict]) -> None:
        super().__init__(errors[0]["msg"] if errors else "invalid XML batch")
        self.errors = errors


def _error(kind: str, loc: tuple, msg: str, value: object = None) -> dict:
    return {"type": kind, "loc": ("body", *loc), "msg": msg, "input": value}


class _Errors:
    """Collected structure errors; the first ``MAX_ERRORS`` stop the conversion."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, kind: str, loc: tuple, msg: str, value: object = None) -> None:
        self.items.append(_error(kind, loc, msg, value))
        if len(self.items) >= MAX_ERRORS:
            raise BatchXmlError(self.items)


def _blank(text: str | None) -> bool:
    return text is None or not text.strip()


def _leaf(element: Element, loc: tuple, errors: _Errors) -> str:
    """Text of a field element; structure errors are collected."""
    if element.attrib:
        errors.add("xml_structure", loc, "a field element has no attributes", dict(element.attrib))
    if len(element):
        errors.add("xml_structure", loc, "a field element holds text only")
    return element.text or ""


def _record(element: Element, index: int, errors: _Errors) -> dict:
    loc = (RECORDS, index)
    if element.attrib:
        message = "record fields are child elements, not attributes"
        errors.add("xml_structure", loc, message, dict(element.attrib))
    if not _blank(element.text):
        errors.add("xml_structure", loc, "text outside of field elements")
    fields: dict[str, object] = {}
    for child in element:
        name = child.tag
        if not isinstance(name, str) or name.startswith("{"):
            errors.add("xml_structure", loc, "field elements have no namespace", name)
            continue
        if not _blank(child.tail):
            errors.add("xml_structure", loc, "text outside of field elements")
        if name in fields:
            errors.add("xml_duplicate_field", (*loc, name), "field given twice")
            continue
        text = _leaf(child, (*loc, name), errors)
        fields[name] = ALARM_TEXT.get(text, text) if name in ALARM_FIELDS else text
    return fields


def batch_from_xml(data: bytes, max_records: int = MAX_OBSERVATION_RECORDS) -> dict:
    """``{"batch_id": ..., "records": [...]}`` from an XML body; raises BatchXmlError.

    More than ``max_records`` ``<record>`` elements is the contract's ``too_long`` error
    on ``records`` (413 ``too_many_records`` in the API), before any record is read.
    """
    try:
        root = fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException as error:
        message = "DTD, entity declarations and external references are not allowed"
        raise BatchXmlError([_error("xml_forbidden", (), message, type(error).__name__)]) from error
    except ParseError as error:
        line, column = getattr(error, "position", (None, None))
        position = {"line": line, "column": column}
        raise BatchXmlError(
            [_error("xml_invalid", (), "XML is not well-formed", position)]
        ) from error
    if root.tag != ROOT:
        message = "the root element is <batch> without a namespace"
        raise BatchXmlError([_error("xml_structure", (), message, root.tag)])
    count = sum(1 for child in root if child.tag == RECORD)
    if count > max_records:
        message = f"List should have at most {max_records} items after validation, not {count}"
        raise BatchXmlError([_error("too_long", (RECORDS,), message)])
    errors = _Errors()
    body: dict[str, object] = {}
    for name, value in root.attrib.items():
        if name == RECORDS:
            errors.add("xml_structure", (name,), "records are <record> elements")
        else:
            body[name] = value
    if not _blank(root.text):
        errors.add("xml_structure", (), "text outside of elements")
    records: list[dict] = []
    for child in root:
        name = child.tag
        if not _blank(child.tail):
            errors.add("xml_structure", (), "text outside of elements")
        if name == RECORD:
            records.append(_record(child, len(records), errors))
        elif not isinstance(name, str) or name.startswith("{"):
            errors.add("xml_structure", (), "elements have no namespace", name)
        elif name == RECORDS:
            errors.add("xml_structure", (name,), "<record> elements go directly under <batch>")
        elif name in body:
            errors.add("xml_duplicate_field", (name,), "field given twice")
        else:
            body[name] = _leaf(child, (name,), errors)
    if errors.items:
        raise BatchXmlError(errors.items)
    body[RECORDS] = records
    return body


__all__ = ["MAX_ERRORS", "BatchXmlError", "batch_from_xml"]

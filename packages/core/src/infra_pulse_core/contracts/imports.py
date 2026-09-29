"""Uploaded source files: the journal and three references, as CSV or XLSX.

Accepted formats follow the organizers' export: the journal has
``ид_события, ид_канала_данных, дата, время, тревожное, значение_датчика``
(quoted with ``false``/``true`` or unquoted with ``f``/``t``); references are the
channel, object and state files. The journal may also use the ТЗ Appendix 1 header
``ИД записи журнала, ИД канала данных, ИД типа канала данных, Текущее значение,
Дата записи`` (no source alarm: stored as unknown). ``format`` names the kind of
file; the container (CSV or XLSX workbook, first sheet) is recognised by content,
so ``journal_csv`` also accepts an ``.xlsx`` with the same columns. HTTP only stores
the file and queues it; a separate worker parses, loads, recomputes and publishes.
A file is identified by its SHA-256: the same bytes again are a ``duplicate`` and
add no rows.
"""

from typing import Literal, Self

from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    field_validator,
    model_validator,
)

MAX_IMPORT_BYTES = 50 * 1024 * 1024
MAX_QUARANTINE_SAMPLE = 20
# Streaming API batches (C0.2): POST /api/v1/observations.
MAX_OBSERVATION_RECORDS = 5000
MAX_OBSERVATION_BYTES = 5 * 1024 * 1024

ImportFormat = Literal[
    "journal_csv",
    # A batch of journal records received by POST /api/v1/observations, in JSON or
    # XML (source_container); the name is kept for stored imports.
    "journal_json",
    "reference_channels_csv",
    "reference_objects_csv",
    "reference_states_csv",
]
ImportStatus = Literal[
    "queued",
    "parsing",
    "imported",
    "recomputing",
    "published",
    "duplicate",
    "failed",
]
ImportStage = Literal["store", "parse", "load", "detect", "score", "publish"]
QuarantineReason = Literal[
    "bad_column_count",
    "bad_date",
    "bad_time",
    "bad_bool",
    "empty_channel",
    "bad_encoding",
    "value_too_long",
    # ТЗ Appendix 1: «ИД типа канала данных» contradicts the channel reference type.
    "channel_type_conflict",
]
# Which journal header and container the worker recognised (G1).
ImportLayout = Literal["organizers_export", "tz_appendix1", "api_batch", "reference"]
# An API batch (journal_json) is posted as JSON or XML; the stored body is kept as sent.
ImportContainer = Literal["csv", "xlsx", "json", "xml"]
ImportErrorCode = Literal[
    "file_too_large",
    "unknown_format",
    "bad_header",
    "bad_encoding",
    "no_valid_rows",
    "reference_missing",
    "recompute_failed",
    "internal_error",
]
_TERMINAL = {"published", "duplicate", "failed"}


class ImportStageTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: ImportStage
    seconds: float = Field(ge=0)


class QuarantineSample(BaseModel):
    """A rejected line with its reason; the raw excerpt is shown only to administrators."""

    model_config = ConfigDict(extra="forbid")

    line_no: int = Field(ge=1)
    reason: QuarantineReason
    raw_excerpt: str = Field(max_length=200)


class ImportFile(BaseModel):
    """One uploaded file and its processing report.

    Row counts: ``rows_total = rows_accepted + rows_duplicate + rows_quarantined``.
    ``unknown_channels`` are channels absent from the reference; their rows are kept
    with an unknown object (DATA-04). ``forecast_generation`` is the publication that
    first included the file.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    format: ImportFormat
    file_name: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0, le=MAX_IMPORT_BYTES)
    uploaded_by: str = Field(min_length=1)
    uploaded_at: AwareDatetime
    status: ImportStatus
    finished_at: AwareDatetime | None = None
    rows_total: int | None = Field(default=None, ge=0)
    rows_accepted: int | None = Field(default=None, ge=0)
    rows_duplicate: int | None = Field(default=None, ge=0)
    rows_quarantined: int | None = Field(default=None, ge=0)
    unknown_channels: int | None = Field(default=None, ge=0)
    event_from: AwareDatetime | None = None
    event_to: AwareDatetime | None = None
    reference_version: str | None = None
    duplicate_of: str | None = None
    forecast_generation: int | None = Field(default=None, ge=1)
    error_code: ImportErrorCode | None = None
    timings: list[ImportStageTiming] = Field(default_factory=list)
    quarantine_reasons: dict[QuarantineReason, int] = Field(default_factory=dict)
    quarantine_sample: list[QuarantineSample] = Field(
        default_factory=list, max_length=MAX_QUARANTINE_SAMPLE
    )
    # What the publication after this file changed: «новых: N, снято по событию: M».
    new_card_ids: list[str] = Field(default_factory=list)
    released_card_ids: list[str] = Field(default_factory=list)
    # G1: recognised header and container; records whose source sent no alarm flag
    # (ТЗ Appendix 1) and report lines for the administrator, shown as written.
    source_layout: ImportLayout | None = None
    source_container: ImportContainer | None = None
    alarm_not_provided: int | None = Field(default=None, ge=0)
    notes: list[str] = Field(default_factory=list, max_length=10)
    # Fixture responses are simulated: nothing was stored or processed.
    simulated: bool = False

    @model_validator(mode="after")
    def check_report(self) -> Self:
        if (self.status == "failed") != (self.error_code is not None):
            raise ValueError("failed status and error code go together")
        if (self.status in _TERMINAL) != (self.finished_at is not None):
            raise ValueError("only finished imports carry finished_at")
        if self.finished_at is not None and self.finished_at < self.uploaded_at:
            raise ValueError("import finished before upload")
        if (self.status == "duplicate") != (self.duplicate_of is not None):
            raise ValueError("duplicate status and the original import go together")
        if self.status == "published" and (
            self.format == "journal_csv" and self.forecast_generation is None
        ):
            raise ValueError("a published journal import names its forecast generation")
        counts = (self.rows_accepted, self.rows_duplicate, self.rows_quarantined)
        if self.rows_total is not None and None not in counts:
            if sum(counts) != self.rows_total:
                raise ValueError("row counts do not add up")
        if self.event_from and self.event_to and self.event_from > self.event_to:
            raise ValueError("event period is reversed")
        if any(count < 1 for count in self.quarantine_reasons.values()):
            raise ValueError("quarantine reason counts are positive")
        if self.rows_quarantined is not None and self.quarantine_reasons:
            if sum(self.quarantine_reasons.values()) != self.rows_quarantined:
                raise ValueError("quarantine reasons do not add up")
        if (self.new_card_ids or self.released_card_ids) and self.status != "published":
            raise ValueError("only a published import lists changed cards")
        stages = [timing.stage for timing in self.timings]
        if len(stages) != len(set(stages)):
            raise ValueError("duplicate stage timing")
        return self


class ImportList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ImportFile]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None = None

    @model_validator(mode="after")
    def check_page(self) -> Self:
        if len(self.items) > min(self.total, self.limit):
            raise ValueError("page exceeds its limit or total")
        return self


class ObservationRecord(BaseModel):
    """One journal record with the fields of the CSV export.

    Keys may be given in English or as in the CSV header: ``event_id`` / ``ид_события``,
    ``channel_id`` / ``ид_канала_данных``, ``alarm`` / ``тревожное``, ``value`` /
    ``значение_датчика``. Time is either ``date`` + ``time`` (local Moscow time, the same
    rule as the CSV: ``дата`` ``YYYY-MM-DD`` and ``время`` ``HH:MM:SS``) or ``event_at``
    (ISO 8601 with an offset), never both. ``value`` is the source text kept verbatim.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(
        min_length=1, max_length=64, validation_alias=AliasChoices("event_id", "ид_события")
    )
    channel_id: str = Field(
        min_length=1,
        max_length=64,
        validation_alias=AliasChoices("channel_id", "ид_канала_данных"),
    )
    date: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        validation_alias=AliasChoices("date", "дата"),
    )
    time: str | None = Field(
        default=None,
        pattern=r"^\d{2}:\d{2}:\d{2}$",
        validation_alias=AliasChoices("time", "время"),
    )
    event_at: AwareDatetime | None = None
    alarm: StrictBool = Field(validation_alias=AliasChoices("alarm", "тревожное"))
    value: StrictStr = Field(
        max_length=1000, validation_alias=AliasChoices("value", "значение_датчика")
    )

    @field_validator("event_id", "channel_id", mode="before")
    @classmethod
    def integer_ids_as_text(cls, value):
        if isinstance(value, bool):
            raise ValueError("identifier must be text or an integer")
        return str(value) if isinstance(value, int) else value

    @model_validator(mode="after")
    def check_time(self) -> Self:
        local = self.date is not None or self.time is not None
        if (self.date is None) != (self.time is None):
            raise ValueError("date and time go together")
        if local == (self.event_at is not None):
            raise ValueError("give either date and time or event_at")
        return self


class ObservationBatch(BaseModel):
    """A batch of records from an integration; ``batch_id`` is the client's idempotency key.

    The same ``batch_id`` with the same bytes returns the stored import; with other bytes
    it is a conflict (409). Records are also deduplicated with the CSV import key.
    The body is JSON or the same batch in XML (``<batch batch_id="...">`` with
    ``<record>`` elements whose child elements are these fields); both are validated
    by this model.
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    records: list[ObservationRecord] = Field(min_length=1, max_length=MAX_OBSERVATION_RECORDS)

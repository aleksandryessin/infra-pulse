import hashlib
import importlib.util
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app_role import app_dsn
from fastapi.testclient import TestClient

from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_replay_reference_coverage_keeps_unheard_channels_without_diagnosis(tmp_path):
    dsn = os.environ.get("INFRA_TEST_REPLAY_DSN")
    if not dsn:
        pytest.skip("local PostgreSQL replay integration DSN not provided")
    import pyarrow as pa
    import pyarrow.parquet as pq

    script = Path(__file__).resolve().parents[1] / "scripts" / "load_attention_replay.py"
    spec = importlib.util.spec_from_file_location("load_attention_replay", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    snapshot = tmp_path / "synthetic-accepted"
    event_dir = snapshot / "curated" / "month=2026-09"
    event_dir.mkdir(parents=True)
    event_path = event_dir / "events.parquet"
    roster_path = snapshot / "channels.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "row_uid": "synthetic-row-1",
                    "event_id": 1,
                    "channel_id": 1,
                    "object_id": 10,
                    "sensor_type": "Газовый датчик",
                    "system_type": "Газовый контроль",
                    "value_raw": "Обнаружен газ",
                    "value_numeric": None,
                    "alarm": True,
                    "event_ts_utc": datetime(2026, 9, 24, 9, 1),
                    "source_file": "synthetic.csv",
                    "source_sha256": "synthetic-source-hash",
                    "record_ordinal": 1,
                    "event_ts_local_raw": "24.09.2026 12:01:00",
                    "reference_version": "synthetic-reference-v1",
                    "is_epoch_placeholder": False,
                    "sentinel_candidate": False,
                    "rare_lexeme": False,
                    "ts_group_distinct_values": 1,
                    "reference_status": "matched",
                }
            ]
        ),
        event_path,
    )
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "channel_id": 1,
                    "object_id": 10,
                    "system_type": "Газовый контроль",
                    "sensor_type": "Газовый датчик",
                },
                {
                    "channel_id": 2,
                    "object_id": 10,
                    "system_type": "Газовый контроль",
                    "sensor_type": "Газовый датчик",
                },
                {
                    "channel_id": 3,
                    "object_id": 20,
                    "system_type": "Водоотведение",
                    "sensor_type": "Состояние насоса",
                },
            ]
        ),
        roster_path,
    )
    (snapshot / "manifest.json").write_text(
        json.dumps(
            {
                "publication_status": "accepted",
                "output_sha256": {
                    "curated/month=2026-09/events.parquet": _sha256(event_path),
                    "channels.parquet": _sha256(roster_path),
                },
            }
        ),
        encoding="utf-8",
    )
    start = datetime(2026, 9, 24, 9, tzinfo=UTC)
    end = start + timedelta(minutes=10)
    report = module.load(
        snapshot,
        month="2026-09",
        start=start,
        end=end,
        dsn=dsn,
        namespace_id="synthetic-coverage-test",
    )
    assert report["loaded_rows"] == 1
    assert report["roster_channels"] == 3
    assert (
        module.load(
            snapshot,
            month="2026-09",
            start=start,
            end=end,
            dsn=dsn,
            namespace_id="synthetic-coverage-test",
        )["roster_channels"]
        == 3
    )

    client = TestClient(
        create_app(
            Settings(
                mode="replay",
                db_dsn=app_dsn(dsn),
                replay_namespace="synthetic-coverage-test",
                replay_snapshot_id=report["snapshot_id"],
                _env_file=None,
            )
        )
    )
    params = {"as_of": end.isoformat()}
    objects = client.get("/api/v1/attention/coverage/objects", params=params)
    assert objects.status_code == 200, objects.text
    payload = objects.json()
    assert payload["mapping_basis"] == "current_reference_not_historical"
    assert payload["reference_sha256"] == _sha256(roster_path)
    assert payload["total"] == 2
    by_object = {item["object_id"]: item for item in payload["items"]}
    assert by_object["10"]["reference_channel_count"] == 2
    assert by_object["10"]["heard_channel_count"] == 1
    assert by_object["10"]["record_count"] == 1
    assert by_object["20"]["heard_channel_count"] == 0
    assert by_object["20"]["last_available_at"] is None

    channels = client.get(
        "/api/v1/attention/coverage/channels",
        params={**params, "object_id": "10", "offset": 0, "limit": 25},
    ).json()
    assert channels["total"] == 2
    assert [item["channel_id"] for item in channels["items"]] == ["2", "1"]
    assert channels["items"][0]["coverage_status"] == "no_record_in_replay_window"
    assert channels["items"][0]["last_available_at"] is None
    assert channels["items"][1]["coverage_status"] == "heard_in_replay_window"
    before_event = client.get(
        "/api/v1/attention/coverage/channels",
        params={"as_of": start.isoformat(), "object_id": "10"},
    ).json()
    assert all(item["record_count"] == 0 for item in before_event["items"])
    assert client.get("/api/v1/attention", params=params).json()["all_records_total"] == 1

    roster_path.write_bytes(roster_path.read_bytes() + b"synthetic-corruption")
    with pytest.raises(ValueError, match="channel reference hash differs"):
        module.load(
            snapshot,
            month="2026-09",
            start=start,
            end=end,
            dsn=dsn,
            namespace_id="synthetic-coverage-test",
        )

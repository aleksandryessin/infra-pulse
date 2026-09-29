"""Leakage, censoring, forward split and canonical replay regression checks."""

from datetime import date, timedelta

import numpy as np
import pytest

from infra_pulse_core.features.incident_list import cutoff_of
from infra_pulse_core.features.phase_feeder_episodes import Record
from infra_pulse_research.sequence.data import Vocabulary, dates, prepare_object, split_indices
from infra_pulse_research.sequence.evaluation import paired_intervals, replay
from infra_pulse_research.sequence.training import Tracking


@pytest.fixture
def history():
    day = date(2023, 1, 2)
    t = cutoff_of(day)
    records = [
        Record("c", t - timedelta(days=400), "Есть питание", object_id="1"),
        Record("c", t - timedelta(days=10), "Неисправен", object_id="1"),
        Record("c", t - timedelta(days=9), "Есть питание", object_id="1"),
        Record("c", t - timedelta(hours=1), "Обесточен", object_id="1"),
    ]
    covered = set(dates(day - timedelta(days=401), day + timedelta(days=30)))
    return day, t, records, covered


def test_future_mutations_and_cutoff_are_invisible_to_features(history):
    day, t, records, covered = history
    a = prepare_object(records, covered, day, day + timedelta(days=1), max_steps=5)
    b = prepare_object(
        records
        + [
            Record("new", t, "NOVEL", object_id="1"),
            Record("c", t + timedelta(hours=1), "Неисправен", object_id="1"),
        ],
        covered,
        day,
        day + timedelta(days=1),
        max_steps=5,
    )
    for field in ("states", "numeric", "lengths", "tabular"):
        np.testing.assert_array_equal(getattr(a, field), getattr(b, field))
    assert a.rows[0]["eligible"] == b.rows[0]["eligible"]
    assert a.rows[0]["candidate_channels"] == b.rows[0]["candidate_channels"] == ["c"]
    assert a.rows[0]["y"] == 0 and b.rows[0]["y"] == 1
    assert t.isoformat().endswith("+03:00")


def test_censoring_never_changes_eligibility_or_becomes_negative(history):
    day, t, records, covered = history
    records.append(Record("c", t + timedelta(days=2), "Неисправен", object_id="1"))
    known = prepare_object(records, covered, day, day + timedelta(days=1))
    covered.remove(day + timedelta(days=5))
    unknown = prepare_object(records, covered, day, day + timedelta(days=1))
    assert known.rows[0]["y"] == 1 and unknown.rows[0]["y"] == -1
    assert known.rows[0]["eligible"] == unknown.rows[0]["eligible"] is True
    np.testing.assert_array_equal(known.numeric, unknown.numeric)


def test_padding_truncation_and_object_isolation(history):
    day, t, records, covered = history
    data = prepare_object(records, covered, day, day + timedelta(days=1), max_steps=1)
    assert data.rows[0]["truncated"] and data.states[0, 0] == "Обесточен"
    with pytest.raises(ValueError, match="one known object"):
        prepare_object(
            records + [Record("z", t, "Неисправен", object_id="2")],
            covered,
            day,
            day + timedelta(days=1),
        )
    with pytest.raises(ValueError, match="timezone"):
        prepare_object(
            [Record("c", t.replace(tzinfo=None), "x", object_id="1")],
            covered,
            day,
            day + timedelta(days=1),
        )


def test_vocabulary_is_fit_only_on_train_valid_tokens():
    vocabulary = Vocabulary.fit(np.array([["Есть питание", "PAD_NOISE"]]), np.array([1]))
    out = vocabulary.transform(
        np.array([["Неисправен", "Есть питание", "PAD_NOISE"]]), np.array([2])
    )
    assert out.tolist() == [[1, 2, 0]]
    assert "PAD_NOISE" not in vocabulary.tokens


def test_forward_split_purges_overlapping_outcomes():
    rows = [
        {"day": str(d), "eligible": True, "y": 0} for d in dates(date(2023, 1, 1), date(2023, 7, 1))
    ]
    fold = {
        "train_start": "2023-01-01",
        "validation_start": "2023-03-01",
        "test_start": "2023-05-01",
        "test_end": "2023-07-01",
    }
    split = split_indices(rows, fold)
    assert rows[split["train"][-1]]["day"] == "2023-02-15"
    assert rows[split["validation"][0]]["day"] == "2023-03-01"
    fold["test_start"] = "2022-01-01"
    with pytest.raises(ValueError, match="past to future"):
        split_indices(rows, fold)


def replay_rows():
    rows = []
    for day in dates(date(2023, 1, 1), date(2023, 1, 7)):
        for obj in ("1", "2", "3"):
            event = "2023-01-02T12:00:00+03:00" if obj == "1" and day.day <= 2 else None
            rows.append(
                {
                    "object_id": obj,
                    "day": str(day),
                    "eligible": True,
                    "candidate_channels": [obj],
                    "y": int(event is not None),
                    "first_event": event,
                    "static_score": 1,
                }
            )
    return rows


def test_release_is_event_time_and_limit_is_concurrent_not_daily():
    rows = replay_rows()
    scores = np.array(
        [
            3
            if r["object_id"] == "1" and r["day"] <= "2023-01-02"
            else (2 if r["object_id"] == "2" else 1)
            for r in rows
        ]
    )
    events = [
        {
            "object_id": "1",
            "event_id": "e",
            "at": "2023-01-02T12:00:00+03:00",
            "channels": ["1"],
            "covered": True,
        }
    ]
    m, blocks = replay(rows, scores, events, start="2023-01-01", end="2023-02-01", k=1)
    assert m["cards_issued"] == 2 and m["max_open"] == 1
    assert m["card_precision"] == 0.5 and m["incident_recall"] == 1
    assert paired_intervals(blocks, blocks, seed=17)["precision_delta_95"] == [0.0, 0.0]


def test_unknown_cards_are_issued_but_excluded_from_precision():
    rows = replay_rows()
    rows[0]["y"] = -1
    m, _ = replay(rows, np.ones(len(rows)), [], start="2023-01-01", end="2023-02-01", k=1)
    assert m["cards_unknown"] == 1
    assert m["cards_known"] == 1


def test_bootstrap_pairs_same_blocks_and_tracking_is_local_only():
    blocks = [{"week": "a", "counts": [2, 3, 1, 2]}, {"week": "b", "counts": [1, 4, 2, 5]}]
    result = paired_intervals(blocks, blocks, seed=17)
    assert result["precision_delta_95"] == [0.0, 0.0]
    assert result["recall_delta_95"] == [0.0, 0.0]
    with pytest.raises(ValueError, match="loopback"):
        Tracking("https://example.com")


def test_source_adapter_uses_existing_curated_contract_and_overlay(tmp_path):
    import json

    import pyarrow as pa
    import pyarrow.parquet as pq

    from infra_pulse_research.sequence.io import ROOT, load_config, preflight, prepare_snapshot
    from infra_pulse_research.sequence.training import digest

    config = load_config(ROOT / "data-science/configs/sequence_research_v1.json")
    config["folds"] = [
        {
            "name": "fixture",
            "train_start": "2023-01-01",
            "validation_start": "2023-03-01",
            "test_start": "2023-05-01",
            "test_end": "2023-07-01",
        }
    ]
    snapshot, overlay = tmp_path / "snapshot", tmp_path / "overlay"
    partition = snapshot / "curated/month=2022-01"
    partition.mkdir(parents=True)
    overlay.mkdir()
    manifest = snapshot / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "publication_status": "accepted",
                "split_reservations": {"fixture": ["2022-01-01", "2023-07-01"]},
            }
        )
    )
    (overlay / "manifest.json").write_text(
        json.dumps(
            {
                "base_manifest_sha256": digest(manifest),
                "status": "accepted_research_overlay_not_training_approval",
                "rules": [
                    {"sensor_type": "Состояние фазы", "start": "2023-05-10", "end": "2023-05-11"}
                ],
            }
        )
    )
    events = [
        {
            "channel_id": "c",
            "object_id": "o",
            "event_ts_local_raw": str(d) + " 06:00:00",
            "sensor_type": "Состояние фазы",
            "value_raw": "Есть питание",
            "alarm": False,
            "is_epoch_placeholder": False,
        }
        for d in dates(date(2022, 1, 1), date(2023, 7, 1))
    ]
    events[360]["value_raw"] = "Неисправен"
    pq.write_table(pa.Table.from_pylist(events), partition / "events.parquet")
    pq.write_table(
        pa.Table.from_pylist(
            [{"channel_id": "c", "object_id": "o", "sensor_type": "Состояние фазы"}]
        ),
        snapshot / "channels.parquet",
    )
    pq.write_table(
        pa.Table.from_pylist(
            [{"day": d, "working_covered": True} for d in dates(date(2022, 1, 1), date(2023, 7, 1))]
        ),
        snapshot / "coverage.parquet",
    )
    pq.write_table(
        pa.Table.from_pylist([{"day": date(2023, 5, 10), "sensor_type_scope": "Состояние фазы"}]),
        overlay / "policy_days.parquet",
    )
    config.update(snapshot=str(snapshot), overlay=str(overlay / "manifest.json"))
    assert preflight(config)["objects"] == 1
    data, meta = prepare_snapshot(config)
    assert meta["status"] == "prepared" and meta["input_sha256"]
    may = next(r for r in data.rows if r["day"] == "2023-05-01")
    assert may["eligible"] and may["y"] == -1
    config["max_rows_per_object"] = 10
    with pytest.raises(ValueError, match="observation budget"):
        prepare_snapshot(config)

"""Meaningful temporal, semantic and policy regressions for registered episodes."""

import json
from datetime import date

import numpy as np
import pandas as pd

from infra_pulse_research.episodes.data import build, split, variant
from infra_pulse_research.episodes.evaluation import gate, intervals, replay
from infra_pulse_research.episodes.protocol import (
    FAMILIES,
    derive_events,
    episode_heads,
    normalize,
    sessions,
)


def runs(rows):
    frame = normalize(pd.DataFrame(rows))
    frame = frame.sort_values(["channel_id", "at"])
    frame["prev_at"] = frame.groupby("channel_id").at.shift()
    frame["prev_state"] = frame.groupby("channel_id").state.shift()
    frame = frame[frame.state.ne(frame.prev_state)].copy()
    frame["next_at"] = frame.groupby("channel_id").at.shift(-1)
    frame["next_state"] = frame.groupby("channel_id").state.shift(-1)
    return frame


def record(at, state, channel="c", sensor="Датчик дыма", alarm=False):
    return dict(
        object_id="o", channel_id=channel, sensor_type=sensor, at=at, state=state, alarm=alarm
    )


def cov():
    return {f: set(pd.date_range("2022-01-01", "2027-01-01").date) for f in set(FAMILIES.values())}


def test_motion_activation_is_not_fault_and_fault_is_retained():
    events = derive_events(
        runs(
            [
                record("2024-01-01", "Движения нет", sensor="Датчик движения"),
                record("2024-01-02", "Обнаружено движение", sensor="Датчик движения"),
                record("2024-01-03", "Неисправен", sensor="Датчик движения"),
            ]
        ),
        cov(),
    )
    assert [(e["component"], e["family"]) for e in events] == [("A", "motion")]


def test_conflicts_unknown_and_missing_days_cannot_create_transition():
    rows = [
        record("2024-01-01", "Дыма нет"),
        record("2024-01-02", "Норма"),
        record("2024-01-02", "Неисправен"),
        record("2024-01-03", "Неисправен"),
    ]
    assert derive_events(runs(rows), cov()) == []
    coverage = cov()
    coverage["smoke"].remove(date(2024, 1, 2))
    assert derive_events(runs([rows[0], rows[-1]]), coverage) == []


def test_reset_requires_normal_return_and_delayed_isolation():
    rows = [record("2024-01-01 12:00", "Обнаружен дым"), record("2024-01-01 12:01", "Дыма нет")]
    e = derive_events(runs(rows), cov())[0]
    assert e["component"] == "B" and e["available_at"] == "2024-01-01T12:10:00"
    rows[-1]["state"] = "Неисправен"
    assert not any(e["component"] == "B" for e in derive_events(runs(rows), cov()))
    rows[-1]["state"] = "Дыма нет"
    rows.append(record("2024-01-01 12:09", "Обнаружено движение", "other", "Датчик движения"))
    assert derive_events(runs(rows), cov()) == []


def test_structural_session_survives_alarm_drift():
    rows = [record(f"2024-01-01 12:0{i}", "Обнаружен дым", str(i)) for i in range(5)]
    before = sessions(runs(rows))
    after = sessions(runs([dict(r, alarm=True) for r in rows]))
    assert len(before) == len(after) == 1
    assert before[0]["legacy"] and not after[0]["legacy"]
    assert after[0]["status"] == "inferred_work_like_not_confirmed"


def test_episode_members_and_24h_chain():
    events = [
        dict(object_id="o", family="smoke", channel_id=str(i), at=t, available_at=t, component="A")
        for i, t in enumerate(["2024-01-01T01:00", "2024-01-01T02:00", "2024-01-02T02:00"])
    ]
    heads = list(episode_heads(events))
    assert len(heads) == 2 and len(heads[0]["members"]) == 2


def fixture_directory(tmp_path):
    days = pd.date_range("2022-01-01", "2023-07-01", inclusive="left")
    daily = pd.DataFrame(
        [
            dict(
                object_id="o",
                sensor_type="Датчик дыма",
                day=d,
                messages=2,
                transitions=1,
                channels=1,
                alarm_share=0.0,
                fault_share=0.0,
                activation_share=0.0,
                unknown_share=0.0,
                mean_gap_hours=1.0,
                max_gap_hours=1.0,
            )
            for d in days
        ]
    )
    daily.to_parquet(tmp_path / "daily.parquet", index=False)
    event = dict(
        object_id="o",
        family="smoke",
        channel_id="c",
        component="B",
        at="2023-02-01T23:59:00",
        available_at="2023-02-02T00:09:00",
    )
    private = {
        "coverage": {f: [str(d.date()) for d in days] for f in set(FAMILIES.values())},
        "events": [event],
        "sessions": [],
    }
    (tmp_path / "private.json").write_text(json.dumps(private))
    config = {
        "history_days": 90,
        "max_tensor_bytes": 100000000,
        "folds": [
            dict(
                name="f",
                train_start="2023-01-01",
                validation_start="2023-03-01",
                test_start="2023-04-01",
                test_end="2023-07-01",
            )
        ],
    }
    return config, private, daily


def test_full_90days_cutoff_future_mutation_and_delayed_labels(tmp_path):
    config, private, daily = fixture_directory(tmp_path)
    data = build(config, tmp_path, "B", 14, "none")
    idx = next(i for i, r in enumerate(data.rows) if r["day"] == "2023-02-02")
    before = data.numeric[idx].copy()
    assert data.lengths[idx] == 90 and data.rows[idx]["static_score"] == 0
    private["events"].append(
        dict(private["events"][0], at="2023-03-01T01:00:00", available_at="2023-03-01T01:10:00")
    )
    (tmp_path / "private.json").write_text(json.dumps(private))
    daily.loc[daily.day >= "2023-02-02", "messages"] = 9999
    daily.to_parquet(tmp_path / "daily.parquet", index=False)
    changed = build(config, tmp_path, "B", 14, "none")
    np.testing.assert_array_equal(before, changed.numeric[idx])
    assert changed.rows[idx]["eligible"] == data.rows[idx]["eligible"]


def test_future_work_mask_changes_known_not_eligibility(tmp_path):
    config, private, _ = fixture_directory(tmp_path)
    data = build(config, tmp_path, "B", 14, "none")
    private["sessions"] = [dict(object_id="o", start="2023-02-02T12:00", end="2023-02-02T12:10")]
    (tmp_path / "private.json").write_text(json.dumps(private))
    censored = variant(data, tmp_path, "B", 14, "structural_unknown")
    idx = next(i for i, r in enumerate(data.rows) if r["day"] == "2023-02-01")
    assert censored.rows[idx]["y"] == -1 and data.rows[idx]["y"] == 1
    assert censored.rows[idx]["eligible"] == data.rows[idx]["eligible"]
    ix = split(censored.rows, config["folds"][0], 14, "B")
    assert all(
        pd.Timestamp(censored.rows[i]["day"]) + pd.Timedelta(days=15) <= pd.Timestamp("2023-03-01")
        for i in ix["train"]
    )


def test_shared_budget_new_heads_and_no_future_candidate_requirement():
    rows = [
        dict(object_id=str(o), family=f, day=str(d.date()), eligible=True, y=0)
        for d in pd.date_range("2024-01-01", "2024-01-31")
        for o in range(12)
        for f in ("smoke", "motion")
    ]
    head = dict(
        object_id="0",
        family="motion",
        at="2024-01-02T01:00",
        available_at="2024-01-02T01:00",
        covered=True,
        uncertain=False,
    )
    result = replay(
        rows,
        np.zeros(len(rows)),
        [head],
        dict(test_start="2024-01-01", test_end="2024-02-01"),
        horizon=14,
        mask="none",
    )
    assert result["metrics"]["max_open"] == 10
    assert result["metrics"]["cards_positive"] == result["metrics"]["incident_heads_caught"] == 1
    assert result["metrics"]["incident_recall"] == 1


def test_paired_intervals_identical_and_gate_rejects_no_gain():
    blocks = {u: {"a": [7, 10, 5, 10], "b": [8, 10, 7, 10]} for u in ("days28", "objects")}
    for unit in intervals(blocks, blocks, replicates=50).values():
        assert unit["precision_delta_95"] == unit["recall_delta_95"] == [0.0, 0.0]
    item = {"blocks": blocks, "metrics": {"card_precision": 0.75, "incident_recall": 0.6}}
    result = gate(
        [item] * 3,
        {"frequency": item, "persistence": item},
        dict(recall_gain=0.02, precision_loss=0.01, absolute_precision=0.7, absolute_recall=0.5),
        50,
    )
    assert result["absolute_point_pass"] and not result["passed"]


def test_sql_extraction_timestamp_conflicts_and_overlay(tmp_path, monkeypatch):
    from infra_pulse_research.episodes.data import extract
    from infra_pulse_research.sequence import io as sequence_io
    from infra_pulse_research.sequence.training import digest

    # The run identity asks Git for HEAD and a dirty flag; a checkout without history
    # (an archive download) has neither, so the test pins both answers.
    def fake_git(args, **kwargs):
        return "0" * 40 if "rev-parse" in args else ""

    monkeypatch.setattr(sequence_io.subprocess, "check_output", fake_git)

    snapshot, overlay = tmp_path / "snapshot", tmp_path / "overlay"
    partition = snapshot / "curated/month=2023-01"
    partition.mkdir(parents=True)
    overlay.mkdir()
    manifest = snapshot / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "publication_status": "accepted",
                "split_reservations": {"fixture": ["2022-01-01", "2024-01-01"]},
            }
        )
    )
    (overlay / "manifest.json").write_text(
        json.dumps(
            {
                "base_manifest_sha256": digest(manifest),
                "status": "accepted_research_overlay_not_training_approval",
                "rules": [],
            }
        )
    )
    events = [
        dict(
            channel_id="c",
            object_id="o",
            sensor_type="Датчик дыма",
            event_ts_local_raw=t,
            value_raw=s,
            value_numeric=None,
            alarm=False,
            is_epoch_placeholder=False,
        )
        for t, s in [
            ("2023-01-01 00:00:00", "Дыма нет"),
            ("2023-01-02 00:00:00", "Неисправен"),
            ("2023-01-03 00:00:00", "Норма"),
            ("2023-01-03 00:00:00", "Неисправен"),
        ]
    ]
    pd.DataFrame(events).to_parquet(partition / "events.parquet", index=False)
    pd.DataFrame([dict(channel_id="c", object_id="o", sensor_type="Датчик дыма")]).to_parquet(
        snapshot / "channels.parquet", index=False
    )
    pd.DataFrame(
        {"day": pd.date_range("2022-01-01", "2024-01-01").date, "working_covered": True}
    ).to_parquet(snapshot / "coverage.parquet", index=False)
    pd.DataFrame(
        {
            "day": pd.Series([], dtype="datetime64[ns]"),
            "sensor_type_scope": pd.Series([], dtype="str"),
        }
    ).to_parquet(overlay / "policy_days.parquet", index=False)
    cfg = {
        "snapshot": str(snapshot),
        "overlay": str(overlay / "manifest.json"),
        "training": {"threads": 1},
        "folds": [{"train_start": "2023-01-01", "test_end": "2024-01-01"}],
    }
    out = tmp_path / "out"
    meta = extract(cfg, out)
    raw = json.loads((out / "private.json").read_text())
    assert meta["rows"] == 4 and len(raw["events"]) == 1
    assert "<CONFLICT>" in set(pd.read_parquet(out / "runs.parquet").state)


def test_isolation_uses_same_units_for_microsecond_arrow_timestamps():
    frame = runs(
        [
            record("2024-01-01 12:00", "Обнаружен дым"),
            record("2024-01-01 12:01", "Дыма нет"),
            record("2024-01-01 12:09", "Обнаружено движение", "other", "Датчик движения"),
        ]
    )
    for col in ("at", "prev_at", "next_at"):
        frame[col] = frame[col].astype("datetime64[us]")
    assert derive_events(frame, cov()) == []


def test_public_summary_rejects_partial_and_omits_private_crosswalk(tmp_path):
    import runpy

    import pytest

    from infra_pulse_research.sequence.io import ROOT

    summarize = runpy.run_path(str(ROOT / "data-science/scripts/summarize_episode_research.py"))[
        "summarize"
    ]
    (tmp_path / "run.json").write_text(json.dumps({"status": "running"}))
    with pytest.raises(ValueError, match="partial"):
        summarize(tmp_path, tmp_path, tmp_path / "public.json")
    private_id = "PRIVATE_OBJECT_NEVER_PUBLISH_93875"
    (tmp_path / "private.json").write_text(json.dumps({"events": []}))
    (tmp_path / "manifest.json").write_text("{}")
    (tmp_path / "audit.json").write_text(
        json.dumps(
            {
                "baseline_reconciliation": {
                    "sequence_run_sha256": "fixture",
                    "sequence_signature": "fixture",
                    "folds": [],
                    "original_models": [{"object_id": private_id}],
                },
                "maintenance_by_month": [],
                "event_components": [],
                "fire_risk": {},
                "schedules": {"private_mapping": private_id},
            }
        )
    )
    (tmp_path / "run.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "models": [],
                "config": {"folds": []},
                "gates": [],
                "conclusions": [],
                "seconds": 1,
                "identity": {"fixture": True},
            }
        )
    )
    summarize(tmp_path, tmp_path, tmp_path / "public.json")
    assert private_id not in (tmp_path / "public.json").read_text()

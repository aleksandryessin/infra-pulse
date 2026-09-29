"""TO-session mask applied to built labels, and the object × fire subsystem labels (v9)."""

import numpy as np
import pandas as pd

from infra_pulse_research.modeling import sensor_failure_service_mask as sm

T = pd.Timestamp("2024-03-04")  # Monday
SMOKE, HEAT, PHASE = "Датчик дыма", "Тепловой датчик", "Состояние фазы"


def _at(h, m=0):
    return T + pd.Timedelta(hours=h, minutes=m)


def _labels():
    # Object 1: session 10:00-10:20 -> mask [09:30, 10:50].
    # e1 10:50 smoke (masked, boundary), e2 11:00 smoke (outside), e3 10:05 phase (other
    # type, never masked), e4 10:10 smoke + heat (both masked), e5 smoke with members at
    # 10:40 (masked) and 10:55 (outside: the pair keeps the event). Object 2: no session.
    members = pd.DataFrame(
        {
            "event_id": ["e1", "e2", "e3", "e4", "e4", "e5", "e5", "e6"],
            "object_id": [1, 1, 1, 1, 1, 1, 1, 2],
            "sensor_type": [SMOKE, SMOKE, PHASE, SMOKE, HEAT, SMOKE, SMOKE, SMOKE],
            "start_at": [
                _at(10, 50),
                _at(11),
                _at(10, 5),
                _at(10, 10),
                _at(10, 10),
                _at(10, 40),
                _at(10, 55),
                _at(10, 15),
            ],
            "qualifying": True,
        }
    )
    sessions = pd.DataFrame(
        {"object_id": [1], "session_start": [_at(10)], "session_end": [_at(10, 20)]}
    )
    events = (
        members.groupby("event_id")
        .agg(object_id=("object_id", "first"), event_start=("start_at", "min"))
        .reset_index()
        .assign(qualifies=True)
    )
    pairs = members[["event_id", "object_id", "sensor_type"]].drop_duplicates()
    links = pairs.merge(events[["event_id", "event_start"]], on="event_id").assign(
        issued_at=T, candidate_member=True
    )
    cards = (
        pairs[["object_id", "sensor_type"]]
        .drop_duplicates()
        .assign(issued_at=T, outcome="positive", y_card=1.0, events_in_window=0)
        .reset_index(drop=True)
    )
    cards.loc[cards.object_id == 2, "outcome"] = "excluded"
    cards.loc[cards.object_id == 2, "y_card"] = np.nan
    return members, sessions, events, links, cards


def test_masked_members_are_smoke_and_heat_inside_the_widened_session():
    members, sessions, *_ = _labels()
    got = sm.masked_members(members, sessions)
    assert got.tolist() == [True, False, False, True, True, True, False, False]
    assert not sm.masked_members(members, sessions.iloc[:0]).any()


def test_apply_mask_drops_masked_pairs_and_recomputes_the_cards():
    members, sessions, events, links, cards = _labels()
    masked = sm.masked_members(members, sessions)
    new_cards, new_links, kept = sm.apply_mask(cards, links, events, members, masked)
    got = set(zip(new_links.event_id, new_links.sensor_type, strict=True))
    assert got == {("e2", SMOKE), ("e3", PHASE), ("e5", SMOKE), ("e6", SMOKE)}
    c = new_cards.set_index(["object_id", "sensor_type"])
    assert c.loc[(1, SMOKE), "events_in_window"] == 2  # e2 and e5
    assert c.loc[(1, SMOKE), "first_event_start"] == _at(10, 40)
    assert c.loc[(1, HEAT), "outcome"] == "negative" and c.loc[(1, HEAT), "y_card"] == 0.0
    assert c.loc[(1, PHASE), "outcome"] == "positive"
    assert c.loc[(2, SMOKE), "outcome"] == "excluded" and np.isnan(c.loc[(2, SMOKE), "y_card"])
    assert c.loc[(2, SMOKE), "first_event_start"] == _at(10, 15)
    assert len(kept) == len(members) - 4


def test_sessions_come_from_the_maintenance_module():
    # Five smoke channels of object 1 activated within minutes on a Monday morning, no alarm.
    runs = pd.DataFrame(
        {
            "channel_id": [11, 12, 13, 14, 15],
            "object_id": 1,
            "sensor_type": SMOKE,
            "state": "Обнаружен дым",
            "start": [_at(10, m) for m in (0, 3, 6, 9, 12)],
            "alarm_first": False,
            "next_start": [_at(10, m + 1) for m in (0, 3, 6, 9, 12)],
        }
    )
    s = sm.maintenance_sessions(runs)
    assert len(s) == 1 and s.loc[0, "session_start"] == _at(10)
    assert s.loc[0, "session_end"] == _at(10, 12)
    assert sm.maintenance_sessions(runs.iloc[:4]).empty  # fewer than 5 channels


def test_subsystem_labels_join_smoke_and_heat_of_an_object():
    days = [T + pd.Timedelta(days=d) for d in range(3)]
    pair_cards = pd.DataFrame(
        {
            "object_id": [1] * 6 + [2] * 3,
            "sensor_type": [SMOKE] * 3 + [HEAT] * 3 + [SMOKE] * 3,
            "issued_at": days * 3,
            "exclusion_reason": [
                "no_candidate_channels",
                None,
                "source_coverage",
                "no_candidate_channels",
                None,
                "source_coverage",
                None,
                "split_boundary",
                None,
            ],
        }
    )
    events = pd.DataFrame(
        {
            "object_id": [1, 1, 1, 2],
            "start_at": [_at(30), _at(30), T + pd.Timedelta(days=5), _at(5)],
        }
    )
    cards, links, ev, members = sm.subsystem_labels(pair_cards, events, days=2)
    assert len(ev) == 3  # two events of object 1 at the same moment are one
    c = cards.set_index(["object_id", "issued_at"])
    # Day 0 of object 1: only 'no candidate' reasons -> evaluable, the 30 h event inside.
    assert c.loc[(1, days[0]), "outcome"] == "positive"
    assert c.loc[(1, days[0]), "first_event_start"] == _at(30)
    assert c.loc[(1, days[1]), "outcome"] == "positive"  # [day 1, day 3) holds 30 h
    assert c.loc[(1, days[2]), "exclusion_reason"] == "source_coverage"
    assert c.loc[(2, days[0]), "outcome"] == "positive"
    assert c.loc[(2, days[1]), "outcome"] == "excluded"
    assert (cards["sensor_type"] == sm.SUBSYSTEM).all()
    assert set(links["event_id"]) <= set(ev["event_id"]) and links["candidate_member"].all()
    assert (members["sensor_type"] == sm.SUBSYSTEM).all() and len(members) == len(ev)

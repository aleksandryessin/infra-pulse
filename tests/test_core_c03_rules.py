"""C0.3 rules on synthetic data: the named link from a channel name, no card for a pair
without events in the past 365 days, user-facing wording of the power-line incident; the
card recommendation v5 (main text, details by repeat and line groups, «аварийное освещение»)."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from infra_pulse_backend.api import forecast_fixture
from infra_pulse_core.contracts.forecast import (
    FEEDER_EVENT_LABEL,
    FEEDER_TARGET,
    TARGET_LABELS,
    ForecastRecommendation,
    check_card_wording,
)
from infra_pulse_core.contracts.scheme import SchemeNamedLink
from infra_pulse_core.features import channel_names as cn
from infra_pulse_core.features import incident_list as il

MSK = timezone(timedelta(hours=3))


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("ФВ2 (В23)", "В23"),
        ("ФРО1 (ГРО1-6)", "ГРО1-6"),
        ("Фидер ФРО1 (ГРО11-17)", "ГРО11-17"),
        ("ФВ9 (В38) ПК12", "В38"),
        ("ФРО (ГРО 1 – 6)", "ГРО1-6"),
        ("ГРО2 ПК24 (ПК9-ПК24)", None),
        ("ФАНС1 ПК24", None),
        ("Резерв (3)", None),
        ("", None),
        (None, None),
    ],
)
def test_named_link_from_channel_name(name, label):
    assert cn.parse_named_link(name) == label


def test_layout_carries_named_link_and_new_version():
    layout = cn.classify_channel("ФВ2 (В23) ПК21")
    assert (layout.feeder_kind, layout.named_link, layout.picket.picket_from) == (
        "ventilation",
        "В23",
        21.0,
    )
    assert cn.NAMED_LINK_RULE_VERSION in layout.version
    assert cn.DICTIONARY_VERSION.endswith("v2")
    assert cn.classify_channel("Ввод 1 (В23)").named_link is None  # landmarks carry none
    with pytest.raises(ValidationError):
        SchemeNamedLink(label="")
    with pytest.raises(ValidationError):
        SchemeNamedLink(label="В23", basis="reference")


def _pair(object_id: str, score: int) -> il.PairAtCutoff:
    return il.PairAtCutoff(
        object_id=object_id, score=score, eligible=True, candidate_channels=frozenset({"c"})
    )


def test_pair_without_past_events_gets_no_card():
    t = datetime(2026, 9, 24, tzinfo=MSK)
    pairs = [_pair("1", 5), _pair("2", 0), _pair("3", 1), _pair("4", 0)]
    chosen = il.select_new(t, pairs, [], k=10)
    assert [pair.object_id for pair, _, _ in chosen] == ["1", "3"]  # fewer than K
    assert [rank for _, rank, _ in chosen] == [1, 2]
    held = [il.IssuedCard(object_id="1", issued_at=t - timedelta(days=1), release_at=None)]
    assert [pair.object_id for pair, _, _ in il.select_new(t, pairs, held, k=10)] == ["3"]
    assert il.select_new(t, [_pair("5", 0)], [], k=10) == []
    assert il.MIN_EVENTS_FOR_CARD == 1 and il.LIST_POLICY_VERSION.endswith("v2")


def test_power_line_wording_in_labels_and_fixture():
    assert TARGET_LABELS[FEEDER_TARGET] == "обесточивание электрооборудования объекта"
    assert FEEDER_EVENT_LABEL.startswith("Линии питания освещения, вентиляции и насосов")
    # v5: «линии из карточки» (the repeated «электропитания» is dropped), never «фидер».
    assert "линии из карточки" in il.RECOMMENDATION_TEXT
    assert "фидер" not in il.RECOMMENDATION_TEXT.lower()
    document = json.dumps(forecast_fixture.forecast_fixture_document(), ensure_ascii=False)
    assert "фидер" not in document.lower()
    scheme = forecast_fixture.object_scheme("synthetic-object-19")
    links = {feeder.name: feeder.named_link for feeder in scheme.feeders}
    assert links["Синт. ФВ2 (В23) ПК21"].label == "В23"
    assert links["Синт. ФРО1 (ГРО1-6) ПК12-ПК18"].label == "ГРО1-6"
    assert links["Синт. ФАНС1 ПК24"] is None


def test_recommendation_v5_main_text_names_the_utility_and_fits_the_contract():
    """v4 (29.09): customer answer «сбой может быть у ресурсоснабжающей организации»; v5 keeps
    it and one main text for every scored card (no level branch, ``decision_code`` None)."""
    assert il.RECOMMENDATION_POLICY_VERSION == "phase-feeders-recommendation-v5"
    text = il.RECOMMENDATION_TEXT
    assert len(text) <= 300
    assert "щит объекта (ЩАП/АВР)" in text
    assert "уточнить плановые отключения у ресурсоснабжающей организации" in text
    assert text.startswith("Передать дежурному энергетику для осмотра до срока карточки")
    assert text.endswith("Результат записать в карточке")
    check_card_wording(text)
    for detail in (
        *il.RECOMMENDATION_GROUP_TEXT.values(),
        il.RECOMMENDATION_REPEAT_TEXT.format(n=15),
    ):
        assert len(detail) <= 160
        check_card_wording(detail)
    ForecastRecommendation(text=text, policy_version=il.RECOMMENDATION_POLICY_VERSION)
    # The fixture calls the same rule under its synthetic policy name.
    assert forecast_fixture.RECOMMENDATION_POLICY == "synthetic-feeder-policy-v5"
    view = forecast_fixture.forecast_card("synthetic-feeders-14d-011")
    assert view.recommendation.text == text
    assert view.recommendation.rule_ids == ["group_pumps", "group_ventilation"]
    assert view.recommendation.decision_code is None
    abstained = forecast_fixture.forecast_card(forecast_fixture.ABSTAINED_ID).recommendation
    assert (abstained.text, abstained.details) == (il.ABSTAINED_RECOMMENDATION_TEXT, [])


def _lines(*names: str) -> list[tuple[str, str | None]]:
    """Card lines with the feeder kind the publisher stores (the versioned dictionary)."""
    return [(name, cn.classify_channel(name).feeder_kind) for name in names]


_P = il.RECOMMENDATION_GROUP_TEXT["pumps"]
_V = il.RECOMMENDATION_GROUP_TEXT["ventilation"]
_T = il.RECOMMENDATION_GROUP_TEXT["heating"]
_O = il.RECOMMENDATION_GROUP_TEXT["ozk"]
_L = il.RECOMMENDATION_GROUP_TEXT["lighting"]
_S = il.RECOMMENDATION_GROUP_TEXT["section"]


def _r(n: int) -> str:
    return il.RECOMMENDATION_REPEAT_TEXT.format(n=n)


# Technologist spec recommendation-v5, section 4: real line names of the stand reference.
# The level of the card does not change the text (level branch not adopted, 29.09).
V5_CASES = [
    (  # 1: pumps and lighting, no repeat
        _lines(
            "ФАНС1 ПК48 Г1 ПК5",
            "ФАНС2 ПК48 Г1 ПК5",
            "ФАНС3 ПК48 Г1 ПК5",
            "ФАО2 ПК48 Г1 ПК5",
            "ФАО1 ПК48 Г1 ПК5",
        ),
        il.Repeat(),
        False,
        [_P, _L],
        ["group_pumps", "group_lighting"],
    ),
    (  # 2: a realized repeat (the spec's n = 3 raised to the agreed n ≥ 5); lighting dropped
        _lines(
            "ФВ1 ПК106", "ФРО1 ПК106 (ПК0-86)", "ФРО2 ПК106 ПК86-156", "ФРО ПК249", "ФАНС2 ПК249"
        ),
        il.Repeat(cards_30d=5, realized=True),
        False,
        [_r(5), _P, _V],
        ["repeat_realized", "group_pumps", "group_ventilation"],
    ),
    (  # 3: heating (ФТС) and lighting; «ОК1 Закрыт» has no group
        _lines(
            "ФТС1 ПК254 щит.",
            "ФТС2 ПК254 щит.",
            "ОК1 Закрыт ПК175",
            "ФАО1 ПК254 щит.",
            "ФАО2 ПК254 щит.",
        ),
        il.Repeat(),
        False,
        [_T, _L],
        ["group_heating", "group_lighting"],
    ),
    (  # 4: only ОЗК — one detail
        _lines(
            "ОЗК Вщ ПК1089 закр.",
            "ОЗК приточн.щит. ПК1089 закр.",
            "ОЗК В15 ПК1153 закр.",
            "ОЗК приточн.щит. ПК28 закр.",
            "ОЗК В3 ПК11 закр.",
        ),
        il.Repeat(),
        False,
        [_O],
        ["group_ozk"],
    ),
    (  # 5: the spec's repeat with n = 2 is below the agreed n ≥ 5: no repeat detail
        _lines(
            "ФАО2 (ПК204-260)",
            "ФРО1 (РО25 - РО28)",
            "ФАО1 (ПК260-316)",
            "ФРО2 (РО22 - РО24)",
            "ФВ1 (В18 - В20)",
        ),
        il.Repeat(cards_30d=2, realized=True),
        False,
        [_V, _L],
        ["group_ventilation", "group_lighting"],
    ),
    (  # 6: ventilation before heating, lighting dropped
        _lines(
            "ФАО2 ПК60 щит.2",
            "ФАО1 ПК60 щит.2",
            "ФВ2 ПК60 щит.2",
            "ФВ1 ПК60 щит.2",
            "ФТС2 ПК60 щит.2",
        ),
        il.Repeat(),
        False,
        [_V, _T],
        ["group_ventilation", "group_heating"],
    ),
    (  # 7: tokens after a digit are read; the section switch comes from the object's layout
        _lines("2ФРО", "1ФРО1", "2ФАО"),
        il.Repeat(),
        True,
        [_L, _S],
        ["group_lighting", "group_section"],
    ),
]


@pytest.mark.parametrize(("lines", "repeat", "section", "details", "rule_ids"), V5_CASES)
def test_recommendation_v5_cases(lines, repeat, section, details, rule_ids):
    rule = il.recommend(True, lines, repeat=repeat, section_switch=section)
    assert rule.text == il.RECOMMENDATION_TEXT
    assert list(rule.details) == details
    assert list(rule.rule_ids) == rule_ids
    assert rule.policy_version == il.RECOMMENDATION_POLICY_VERSION
    recommendation = ForecastRecommendation(
        text=rule.text,
        details=list(rule.details),
        rule_ids=list(rule.rule_ids),
        policy_version=rule.policy_version,
    )
    assert recommendation.decision_code is None and recommendation.regulation_confirmed is False


def test_recommendation_v5_three_details_with_a_frequent_repeat():
    assert il.RECOMMENDATION_REPEAT_MIN_CARDS == 5  # coordinator 29.09: 48.1% of stand cards
    lines = V5_CASES[4][0]  # case 5 with n = 5: repeat, ventilation, lighting
    rule = il.recommend(True, lines, repeat=il.Repeat(cards_30d=5, realized=True))
    assert list(rule.details) == [_r(5), _V, _L]
    assert rule.rule_ids == ("repeat_realized", "group_ventilation", "group_lighting")


def test_recommendation_v5_negative_cases():
    abstained = il.recommend(False, _lines("ФАНС1 ПК24"), repeat=il.Repeat(5, True))
    assert abstained == il.Recommendation(text=il.ABSTAINED_RECOMMENDATION_TEXT)
    other = il.recommend(True, _lines("ПУИ1 ПК12", "Фрез. ПК3", "Резерв 2", "ОК1 Закрыт ПК175"))
    assert (other.text, other.details, other.rule_ids) == (il.RECOMMENDATION_TEXT, (), ())
    # A realized previous card without enough cards in 30 days, or not realized: no repeat.
    assert il.recommend(True, [], repeat=il.Repeat(4, True)).details == ()
    assert il.recommend(True, [], repeat=il.Repeat(3, True)).details == ()
    assert il.recommend(True, [], repeat=il.Repeat(6, False)).details == ()


@pytest.mark.parametrize(
    ("name", "kind", "group"),
    [
        ("ФАНС1 ПК24", "pumps", "pumps"),
        ("ФВ2 (В23) ПК21", "ventilation", "ventilation"),
        ("ОЗК В3 ПК11 закр.", "ozk", "ozk"),
        ("ФОЗК1", "ozk", "ozk"),
        ("ГРО2 ПК24", "lighting", "lighting"),
        ("ФТС1 ПК254 щит.", "other", "heating"),
        ("2ФТС", "other", "heating"),
        ("ПУИ1 ПК12", "other", None),
        ("ОК1 Закрыт ПК175", "other", None),
        ("Резерв 3", "other", None),
        ("ФТСК 1", "other", None),  # not a ФТС token
        ("ФТС1 ПК5", None, "heating"),  # no stored kind: the dictionary decides
        ("ФАНС1", None, "pumps"),
        ("Межсекционный автомат", None, None),  # a landmark is not a card line
    ],
)
def test_recommendation_group_of_a_line(name, kind, group):
    assert cn.recommendation_group(name, kind) == group


def _prior(day: int, *, scored: bool = True, released_day: float | None = None):
    base = datetime(2026, 6, 1, tzinfo=MSK)
    release = None if released_day is None else base + timedelta(days=released_day)
    return il.PriorCard(issued_at=base + timedelta(days=day), scored=scored, release_at=release)


def test_repeat_is_point_in_time():
    t = datetime(2026, 6, 1, tzinfo=MSK) + timedelta(days=30)
    released = _prior(20, released_day=25.5)
    # 3 cards in [t − 30 d, t] with this one; the latest earlier scored card was released.
    cards = [_prior(-1), _prior(10), released, _prior(30)]
    assert il.repeat_at(t, cards) == il.Repeat(cards_30d=3, realized=True)
    # A card issued before the release of the previous one: no realized repeat, although the
    # previous card is released by now (spec, negative test).
    assert il.repeat_at(_prior(24).issued_at, [released]) == il.Repeat(2, False)
    # Only the latest earlier scored card counts; later cards are never read.
    expired_last = [released, _prior(27)]
    assert il.repeat_at(t, expired_last) == il.Repeat(3, False)
    assert il.repeat_at(t, [released, _prior(31, released_day=32)]) == il.Repeat(2, True)
    # An abstained card counts in n but is not «the previous card».
    assert il.repeat_at(t, [released, _prior(28, scored=False)]) == il.Repeat(3, True)
    assert il.repeat_at(t, []) == il.Repeat(1, False)


@pytest.mark.parametrize(
    "text",
    [
        "проверить, что аварийное освещение включается",
        "Аварийное освещение на пути бригады",
        "лампы аварийного освещения",
    ],
)
def test_card_wording_allows_emergency_lighting(text):
    assert check_card_wording(text) == text


@pytest.mark.parametrize(
    "text",
    ["авария на объекте", "аварийная ситуация", "аварийный выезд", "Аварийное отключение"],
)
def test_card_wording_still_rejects_emergency(text):
    with pytest.raises(ValueError, match="авари"):
        check_card_wording(text)


def test_recommendation_contract_checks_details():
    base = {"text": "Проверить линии", "policy_version": "p-v5"}
    ForecastRecommendation(**base, details=[_P, _V, _L], rule_ids=["group_pumps"])
    for bad in (
        {"details": [_P, _V, _L, _S]},  # at most three
        {"details": ["x" * 161]},  # at most 160 characters
        {"details": [""]},
        {"details": ["причина — поломка автомата"]},  # every detail passes the card wording
        {"details": ["авария на линии"]},
        {"rule_ids": ["group_pumps", "group_pumps"]},
        {"rule_ids": ["Group pumps"]},
    ):
        with pytest.raises(ValidationError):
            ForecastRecommendation(**base, **bad)
    assert ForecastRecommendation(**base).details == []

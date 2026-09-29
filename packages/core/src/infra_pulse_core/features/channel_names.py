"""Picket and feeder kind of a «Состояние фазы» channel from its name (pure Python).

The source reference has no topology; 93% of channel names carry a picket («ПК28»,
«ПК86-85»). The scheme shows it as a convention (usually 10 m per picket, rarely another
step — customer answer of 28.09), never as a plan. The picket and the parenthesized token of
a feeder name refer to what the feeder supplies; where it is fed from is not in the name
(customer answer of 28.09), so no connection is inferred.

- **Picket** (``PICKET_RULE_VERSION``): the first «ПК» token of the name. A point
  «ПК28»; a range «ПК86-85» or «ПК451-ПК469» (ordered, ``picket_to >= picket_from``).
  A range in parentheses after a point («ГРО2 ПК24 (ПК9-ПК24)») is not read in v1. No
  token — ``unknown`` (the «ПК ?» column); a picket is never guessed.
- **Role and feeder kind** (``DICTIONARY_VERSION``). Landmarks — inputs («Ввод»), ATS
  («АВР»), panels («ЩАП») and section switches («Межсекционный»): they are orientation
  marks of the scheme and never report «Неисправен» (FINAL_V9). Every other channel is a
  feeder of a kind: lighting — РО, ГРО, ФРО, ФАО; ventilation — ФВ; pumps — ФАНС;
  ОЗК (also «ФОЗК»); anything else — ``other`` (ФТС — a heating-network feeder, ПУИ — an
  indication control panel, both decoded by the customer on 28.09; Фрез., резервные …).
  The dictionary is versioned; names falling to ``other`` are reported for review by
  DS and the technologist, not silently re-mapped.
- **Named link** (``NAMED_LINK_RULE_VERSION``, C0.3): the first parenthesized token of
  the name that is not a picket and looks like «letters + number [range]»: «ФВ2 (В23)» →
  «В23», «ФРО1 (ГРО1-6)» → «ГРО1-6»: what the feeder supplies. It is shown as a label on the
  scheme; no topology and no connection is inferred from it.
- **Recommendation group** (recommendation v5, ``incident_list.recommend``): pumps,
  ventilation, ОЗК and lighting follow the feeder kind; an ``other`` line named ФТС is
  ``heating`` (heating network, customer answer of 28.09); ПУИ, «ОК1 Закрыт», Фрез. and
  reserve lines get no group.
"""

import re
from dataclasses import dataclass
from typing import Literal

DICTIONARY_VERSION = "phase-feeder-kinds-v2"  # v2 (C0.3): layout carries the named link
PICKET_RULE_VERSION = "picket-from-channel-name-v1"
NAMED_LINK_RULE_VERSION = "named-link-from-channel-name-v1"
LAYOUT_VERSION = f"{DICTIONARY_VERSION}+{PICKET_RULE_VERSION}+{NAMED_LINK_RULE_VERSION}"

PicketForm = Literal["point", "range", "unknown"]
FeederKind = Literal["lighting", "ventilation", "pumps", "ozk", "other"]
ChannelRole = Literal["feeder", "landmark"]
LandmarkKind = Literal["input", "ats", "panel", "other"]
RecommendationGroup = Literal["pumps", "ventilation", "heating", "ozk", "lighting"]

_NUMBER = r"(\d+(?:[.,]\d+)?)"
_PICKET = re.compile(rf"ПК\s*{_NUMBER}(?:\s*[-–—]\s*(?:ПК\s*)?{_NUMBER})?", re.IGNORECASE)
# A token starts the name or follows a space, digit, bracket or punctuation.
_START = r"(?:^|(?<=[\s\d(\[.,;:/«\"-]))"
_END = r"(?=$|[\s\d()\[\].,;:/»\"-])"
_LANDMARKS: tuple[tuple[LandmarkKind, re.Pattern[str]], ...] = (
    ("ats", re.compile(rf"{_START}АВР{_END}")),
    ("panel", re.compile(rf"{_START}ЩАП{_END}")),
    ("input", re.compile(rf"{_START}Ввод{_END}")),
    ("other", re.compile(rf"{_START}Межсекц", re.IGNORECASE)),
)
_PARENTHESIZED = re.compile(r"\(([^()]{1,40})\)")
_NAMED_LINK = re.compile(
    r"^([A-Za-zА-Яа-яЁё]{1,6})\s?(\d+(?:[.,]\d+)?)(?:\s*[-–—]\s*(\d+(?:[.,]\d+)?))?$"
)
_FEEDER_KINDS: tuple[tuple[FeederKind, re.Pattern[str]], ...] = (
    ("pumps", re.compile(rf"{_START}ФАНС{_END}")),
    ("ozk", re.compile(rf"{_START}Ф?ОЗК{_END}")),
    ("lighting", re.compile(rf"{_START}(?:ГРО|ФРО|ФАО|РО){_END}")),
    ("ventilation", re.compile(rf"{_START}ФВ{_END}")),
)
_HEATING = re.compile(rf"{_START}ФТС{_END}")
_GROUP_KINDS: frozenset[str] = frozenset({"pumps", "ventilation", "ozk", "lighting"})


@dataclass(frozen=True)
class Picket:
    form: PicketForm
    picket_from: float | None = None
    picket_to: float | None = None
    basis: Literal["channel_name"] | None = None


@dataclass(frozen=True)
class ChannelLayout:
    """Role, feeder kind and picket of one channel by the versioned rules."""

    role: ChannelRole
    feeder_kind: FeederKind | None
    landmark_kind: LandmarkKind | None
    picket: Picket
    named_link: str | None = None
    version: str = LAYOUT_VERSION


UNKNOWN_PICKET = Picket(form="unknown")


def normalize_name(name: str | None) -> str:
    """Trim and collapse whitespace (reference names carry CR/LF and double spaces)."""
    return " ".join((name or "").split())


def parse_picket(name: str | None) -> Picket:
    match = _PICKET.search(normalize_name(name))
    if match is None:
        return UNKNOWN_PICKET
    first = float(match[1].replace(",", "."))
    if match[2] is None:
        return Picket(form="point", picket_from=first, basis="channel_name")
    second = float(match[2].replace(",", "."))
    if second == first:
        return Picket(form="point", picket_from=first, basis="channel_name")
    low, high = sorted((first, second))
    return Picket(form="range", picket_from=low, picket_to=high, basis="channel_name")


def parse_named_link(name: str | None) -> str | None:
    """«ФВ2 (В23)» → «В23»; «ФРО1 (ГРО1-6)» → «ГРО1-6»; pickets in parentheses are skipped."""
    for match in _PARENTHESIZED.finditer(normalize_name(name)):
        token = match[1].strip()
        if re.search(r"ПК", token, re.IGNORECASE):
            continue
        link = _NAMED_LINK.match(token)
        if link is None:
            continue
        label = f"{link[1]}{link[2]}"
        return f"{label}-{link[3]}" if link[3] is not None else label
    return None


def landmark_kind(name: str | None) -> LandmarkKind | None:
    text = normalize_name(name)
    for kind, pattern in _LANDMARKS:
        if pattern.search(text):
            return kind
    return None


def feeder_kind(name: str | None) -> FeederKind:
    text = normalize_name(name)
    for kind, pattern in _FEEDER_KINDS:
        if pattern.search(text):
            return kind
    return "other"


def classify_channel(name: str | None) -> ChannelLayout:
    """Layout of a phase channel; a landmark has no feeder kind."""
    picket = parse_picket(name)
    landmark = landmark_kind(name)
    if landmark is not None:
        return ChannelLayout(
            role="landmark", feeder_kind=None, landmark_kind=landmark, picket=picket
        )
    return ChannelLayout(
        role="feeder",
        feeder_kind=feeder_kind(name),
        landmark_kind=None,
        picket=picket,
        named_link=parse_named_link(name),
    )


def recommendation_group(name: str | None, kind: str | None) -> RecommendationGroup | None:
    """Group of a card line for the recommendation details; ``None`` — no detail.

    The stored feeder kind decides; a card line without one is classified by the same
    versioned dictionary (a landmark never gets a group).
    """
    if kind is None:
        kind = None if landmark_kind(name) is not None else feeder_kind(name)
    if kind in _GROUP_KINDS:
        return kind
    if kind == "other" and _HEATING.search(normalize_name(name)):
        return "heating"
    return None


def name_stem(name: str | None) -> str:
    """Name with numbers masked («ФТС# ПК# щит.#»): a review list without exact pickets."""
    return re.sub(r"\d+(?:[.,]\d+)?", "#", normalize_name(name))

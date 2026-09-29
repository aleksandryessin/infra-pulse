"""ТЗ Appendix 1 «Реестр типов каналов датчиков» against the channel reference (G1).

The sensor type of a record always comes from the loaded channel reference
(``ref_channels.sensor_type``); the Appendix 1 journal's «ИД типа канала данных» is
kept verbatim and only checked. A record whose type ID names a type that the
reference contradicts is quarantined as ``channel_type_conflict`` (decision of the
internal acceptance review, 27.09.2026).

The organizers' reference names types in Russian (``Состояние фазы``), the ТЗ table
by ID and code (``9 phase``). The correspondence below is a **proposal by type
name**, to be confirmed by the technologist; ID 2 («Контакт (не замкнут — норма)»)
covers every contact sensor of the reference. An ID outside this table, a channel
absent from the reference or listed ambiguously is not checked (nothing to
contradict); such records are loaded as usual.
"""

from __future__ import annotations

# ID -> (code, dispatcher name), as printed in ТЗ Appendix 1.
TZ_CHANNEL_TYPES: dict[str, tuple[str, str]] = {
    "2": ("contact-unlock-norm", "Контакт (не замкнут — норма)"),
    "3": ("switch", "Переключатель"),
    "4": ("smoke", "Дым"),
    "5": ("movement", "Движение"),
    "6": ("gas", "Газ"),
    "7": ("pump", "Насос"),
    "8": ("fan", "Вентилятор"),
    "9": ("phase", "Фаза"),
    "12": ("temperature", "Температура"),
}

# ID -> sensor types of the organizers' channel reference that it may denote.
REFERENCE_SENSOR_TYPES: dict[str, frozenset[str]] = {
    "2": frozenset({"КД Дверь", "КД АВ", "КД Люк", "9-секционный люк"}),
    "3": frozenset({"Переключатель"}),
    "4": frozenset({"Датчик дыма"}),
    "5": frozenset({"Датчик движения"}),
    "6": frozenset({"Газовый датчик"}),
    "7": frozenset({"Состояние насоса"}),
    "8": frozenset({"Состояние вентилятора"}),
    "9": frozenset({"Состояние фазы"}),
    "12": frozenset({"Датчик температуры"}),
}


def contradicts(type_id: str | None, reference_sensor_type: str | None) -> bool:
    """True when the Appendix 1 type ID and the reference sensor type disagree."""
    if type_id is None or reference_sensor_type is None:
        return False
    allowed = REFERENCE_SENSOR_TYPES.get(type_id.strip())
    return allowed is not None and reference_sensor_type not in allowed


__all__ = ["REFERENCE_SENSOR_TYPES", "TZ_CHANNEL_TYPES", "contradicts"]

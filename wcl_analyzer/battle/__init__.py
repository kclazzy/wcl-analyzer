"""Battle Analysis — «Бой по шагам»: короткая наглядная картина одного боя.

Время → ключевое событие → действие игрока → карта → сравнение с топом.

Модуль изолирован: на вход — готовый результат сравнения с топом (compare.CompareResult),
на выходе — JSON для страницы. Своего клиента WCL, базы и кэша у модуля нет: данные уже
скачаны и закэшированы общим api.py, координаты приходят в тех же событиях.

    parser        — logs.py (общий): координаты игрока и босса, цели кастов
    positions.py  — шум, координаты относительно босса, упрощение траектории, участки движения
    movement.py   — движения игрока, причина (только подтверждённая), сравнение с топом
    timeline.py   — кандидаты событий → объединение → значимость → 10–20 карточек
    differences.py— 3–5 главных отличий, разрыв DPS, сводка
    model.py      — BattleEvent, общий формат событий
"""
from __future__ import annotations

from .differences import dps_gap, summary, top_differences
from .movement import analyze as analyze_movement
from .timeline import build as build_timeline

VERSION = 1


def build_battle(r) -> dict:
    """CompareResult → данные страницы «Бой по шагам»."""
    me, ref = r.me, r.ref
    mv = analyze_movement(me, ref.logs, ref.name_of)
    picked, cards = build_timeline(r, mv)
    diffs = top_differences(r, mv)
    rel = r.reliability or {}
    weak = [c for c in rel.get("criteria", []) if c.get("level") != "HIGH"]
    mv.pop("_tracks", None)
    return {
        "version": VERSION,
        "boss": me.encounter_name, "duration": round(me.duration, 1), "kill": me.kill,
        "reliability": rel.get("overall"),
        "reliability_reasons": [f"{c['criterion']}: {c['reason']}" for c in weak][:3],
        "ref_label": ref.label, "ref_n": ref.n,
        "phases": [{"t": round(t, 1), "id": pid} for t, pid in me.phases],
        "events": [e.to_dict() for e in picked],
        "all_events": [e.to_dict() for e in cards],
        "movement": mv,
        "differences": diffs,
        "dps": dps_gap(r, diffs),
        "summary": summary(r, mv, diffs),
    }

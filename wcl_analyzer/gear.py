"""Экипировка: уровень предметов по слотам, зачарования и камни — у игрока и у топа.

Какие слоты зачаровываются, не зашито в код: слот считается зачаровываемым, если зачарование
в нём есть у большинства игроков топа. Так таблица не устаревает с новым дополнением.
"""
from __future__ import annotations

from collections import Counter

from .logs import PlayerLog

SLOT_RU = {0: "Голова", 1: "Шея", 2: "Плечи", 4: "Грудь", 5: "Пояс", 6: "Ноги", 7: "Ступни",
           8: "Запястья", 9: "Кисти рук", 10: "Кольцо 1", 11: "Кольцо 2", 12: "Тринкет 1",
           13: "Тринкет 2", 14: "Спина", 15: "Правая рука", 16: "Левая рука", 17: "Дальний бой"}
WEAPON_SLOTS = (15, 16)
ENCHANT_SHARE = 0.5   # доля топа с зачарованием, начиная с которой слот считаем зачаровываемым


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def compare_gear(me: PlayerLog, ref_logs: list[PlayerLog]) -> dict:
    """Таблица экипировки: строка на слот, плюс итог по зачарованиям."""
    refs = [log for log in ref_logs if log.gear]
    by_slot = {s: [] for s in SLOT_RU}
    for log in refs:
        for g in log.gear:
            if g["slot"] in by_slot:
                by_slot[g["slot"]].append(g)
    mine = {g["slot"]: g for g in me.gear}
    rows, missing, missing_temp = [], [], []
    for slot, label in SLOT_RU.items():
        top = by_slot[slot]
        my = mine.get(slot)
        if not my and not top:
            continue
        n_top = len(top)
        ench_share = sum(1 for g in top if g["enchant"]) / n_top if n_top else None
        temp_share = (sum(1 for g in top if g["temp"]) / n_top if n_top else None) if slot in WEAPON_SLOTS else None
        gems_top = _median([g["gems"] for g in top]) if n_top else None
        common = Counter(g["id"] for g in top).most_common(1)
        pop_id, pop_n = common[0] if common else (None, 0)
        pop = next((g for g in top if g["id"] == pop_id), None)
        enchantable = ench_share is not None and ench_share >= ENCHANT_SHARE
        no_ench = bool(my) and enchantable and not my["enchant"]
        no_temp = bool(my) and temp_share is not None and temp_share >= ENCHANT_SHARE and not my["temp"]
        few_gems = bool(my) and gems_top is not None and gems_top >= 1 and my["gems"] < round(gems_top)
        if no_ench:
            missing.append(label)
        if no_temp:
            missing_temp.append(label)
        rows.append({
            "slot": slot, "slot_name": label,
            "id": my["id"] if my else None, "name": (my or {}).get("name"), "icon": (my or {}).get("icon"),
            "bonus": (my or {}).get("bonus") or [],
            "ilvl": my["ilvl"] if my else None, "ref_ilvl": _median([g["ilvl"] for g in top]),
            "enchant": bool(my and my["enchant"]), "enchant_name": (my or {}).get("enchant_name"),
            "enchantable": enchantable, "ref_enchant_share": ench_share,
            "temp": bool(my and my["temp"]) if slot in WEAPON_SLOTS else None, "ref_temp_share": temp_share,
            "gems": my["gems"] if my else None, "ref_gems": gems_top,
            "no_enchant": no_ench, "no_temp": no_temp, "few_gems": few_gems,
            "popular_id": pop_id, "popular_name": (pop or {}).get("name"),
            "popular_share": pop_n / n_top if n_top else None,
            "popular_same": bool(my and pop_id == my["id"]),
        })
    return {"rows": rows, "missing_enchants": missing, "missing_temp": missing_temp,
            "ref_n": len(refs), "has_my": bool(me.gear)}

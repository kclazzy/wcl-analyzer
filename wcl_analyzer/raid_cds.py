"""Рейдовые защитные кулдауны состава: у кого какой есть и через сколько он откатывается.

Источник — состав рейда (класс и спек каждого игрока), а не только нажатия в бою: кулдаун,
который игрок ни разу не нажал, тоже попадает в план. Если в логе есть таланты (CombatantInfo)
и доступен справочник талантов, кулдаун-талант учитывается, только если он взят.
Без данных о талантах учитываются «основные» кулдауны спека (их берут почти всегда) и те,
что игрок нажимал в этом бою.
"""
from __future__ import annotations

from collections import defaultdict

# (класс, спек или None — любой спек): [(id способности, название, перезарядка с, основной)]
SPEC_CDS = {
    ("Priest", "Holy"): [(64843, "Божественный гимн", 180, True), (265202, "Слово Света: Спасение", 720, False),
                         (200183, "Апофеоз", 120, False)],
    ("Priest", "Discipline"): [(62618, "Слово силы: Барьер", 180, True), (47536, "Вознесение", 90, True),
                               (246287, "Евангелизм", 90, False), (421453, "Окончательное покаяние", 240, False)],
    ("Priest", "Shadow"): [(15286, "Вампирские объятия", 120, True)],
    ("Shaman", "Restoration"): [(98008, "Тотем духовной связи", 180, True), (108280, "Тотем целительного потока", 180, True),
                                (114052, "Перерождение", 180, False), (207399, "Тотем защиты предков", 300, False)],
    ("Shaman", "Elemental"): [(108281, "Наставления предков", 120, False)],
    ("Shaman", "Enhancement"): [(108281, "Наставления предков", 120, False)],
    ("Druid", "Restoration"): [(740, "Спокойствие", 180, True), (33891, "Воплощение: Древо Жизни", 180, False),
                               (197721, "Расцвет", 90, False), (391528, "Природная мощь", 120, False)],
    ("Paladin", "Holy"): [(31821, "Владение аурами", 180, True), (216331, "Воин Света", 120, False),
                          (200652, "Избавление Тира", 90, False)],
    ("Monk", "Mistweaver"): [(115310, "Восстановление сил", 180, True), (388615, "Возрождение", 180, False),
                             (322118, "Призыв Юй-лун", 120, False), (325197, "Призыв Цзи-Жэнь", 120, False)],
    ("Evoker", "Preservation"): [(363534, "Перемотка", 240, True), (359816, "Полёт мечты", 120, False),
                                 (370960, "Изумрудное единение", 180, False), (370537, "Стазис", 90, False)],
    ("Evoker", None): [(374227, "Зефир", 120, True)],
    ("Warrior", None): [(97462, "Ободряющий клич", 180, True)],
    ("DeathKnight", None): [(51052, "Зона антимагии", 120, True)],
    ("DemonHunter", None): [(196718, "Мрак", 300, True)],
}
# Сила кулдауна для плана: 3 — большой лечебный/защитный кулдаун на весь рейд,
# 2 — средний, 1 — небольшой. На пик ставится самый сильный свободный.
POWER = {64843: 3, 62618: 3, 98008: 3, 108280: 3, 740: 3, 31821: 3, 115310: 3, 388615: 3, 363534: 3,
         97462: 2, 51052: 2, 196718: 2, 15286: 2, 47536: 2, 246287: 2, 114052: 2, 33891: 2, 391528: 2,
         359816: 2, 370960: 2, 322118: 2, 325197: 2, 216331: 2, 200652: 2, 265202: 2, 200183: 2, 421453: 2,
         207399: 2, 374227: 1, 108281: 1, 197721: 1, 370537: 1}
# Узлы выбора: если взят второй вариант, первого нет
CHOICE_PAIRS = {115310: 388615, 388615: 115310}


def _norm(s) -> str:
    return "".join(ch for ch in str(s or "") if ch.isalnum()).lower()


def candidates(cls: str, spec: str) -> list[tuple]:
    c, s = _norm(cls), _norm(spec)
    out = []
    for (k_cls, k_spec), items in SPEC_CDS.items():
        if _norm(k_cls) == c and (k_spec is None or _norm(k_spec) == s):
            out += items
    return out


def _players(details: dict) -> list[dict]:
    out = []
    for role in ("tanks", "healers", "dps"):
        for p in (details or {}).get(role) or []:
            specs = p.get("specs") or []
            spec = specs[0].get("spec") if specs and isinstance(specs[0], dict) else (specs[0] if specs else "")
            out.append({"id": int(p["id"]), "name": p.get("name", ""), "cls": p.get("type", ""), "spec": spec,
                        "role": {"tanks": "Танк", "healers": "Лекарь", "dps": "DPS"}[role]})
    return out


def _taken_spells(combatant: list, talent_data, players: list[dict]) -> dict[int, set] | None:
    """{игрок: id способностей взятых талантов} по CombatantInfo и справочнику талантов."""
    if not combatant or not talent_data:
        return None
    from .talents import spec_tree
    by_pid = {p["id"]: p for p in players}
    out: dict[int, set] = {}
    for ev in combatant:
        p = by_pid.get(int(ev.get("sourceID", -1)))
        tree = ev.get("talentTree") or []
        if not p or not tree:
            continue
        st = spec_tree(talent_data, p["cls"], p["spec"])
        if not st:
            continue
        spells = set()
        for x in tree:
            nd = st["nodes"].get(int(x.get("nodeID") or 0))
            if not nd:
                continue
            ent = nd["entries"].get(int(x.get("id") or 0))
            if ent and ent[1]:
                spells.add(int(ent[1]))
            elif len(nd["entries"]) == 1:
                sid = next(iter(nd["entries"].values()))[1]
                if sid:
                    spells.add(int(sid))
        out[p["id"]] = spells
    return out


def roster_cds(raw: dict, R: dict, talent_data=None) -> list[dict]:
    """Все рейдовые кулдауны состава с перезарядкой и источником (почему считаем, что он есть)."""
    players = _players(raw.get("details") or {})
    names = {int(a["gameID"]): a.get("name") for a in ((raw.get("report") or {}).get("masterData") or {}).get("abilities") or []}
    pressed = defaultdict(list)
    for c in (R.get("extras") or {}).get("raid_cds") or []:
        if c.get("pid") is not None and c.get("id") is not None:
            pressed[(int(c["pid"]), int(c["id"]))].append(float(c["t"]))
    taken = _taken_spells(raw.get("combatant") or [], talent_data, players)
    dur = float((R.get("info") or {}).get("duration_s") or 0)
    out = []
    for p in players:
        tk = taken.get(p["id"]) if taken is not None else None
        for sid, name_ru, cd, core in candidates(p["cls"], p["spec"]):
            ts = sorted(pressed.get((p["id"], sid), []))
            if ts:
                source = f"нажимал в бою: {len(ts)}"
            elif tk is not None:
                if sid not in tk:
                    continue  # талант не взят
                source = "талант взят, в бою не нажимал"
            elif core:
                if CHOICE_PAIRS.get(sid) and pressed.get((p["id"], CHOICE_PAIRS[sid])):
                    continue  # нажимал второй вариант узла выбора
                source = "обычно есть у спека, в бою не нажимал"
            else:
                continue
            gaps = [b - a for a, b in zip(ts, ts[1:])]
            real_cd = min([cd] + [g for g in gaps if g > 20]) if gaps else cd
            out.append({"pid": p["id"], "player": p["name"], "role": p["role"], "cls": p["cls"], "spec": p["spec"],
                        "id": sid, "name": names.get(sid) or name_ru, "cd": real_cd, "used": len(ts),
                        "max_uses": int(dur // real_cd) + 1 if dur else None, "source": source})
    out.sort(key=lambda c: (c["role"] != "Лекарь", c["player"], -c["cd"]))
    return out

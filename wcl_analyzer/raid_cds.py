"""Рейдовые защитные кулдауны состава: у кого какой есть и через сколько он откатывается.

Источник — состав рейда (класс и спек каждого игрока), а не только нажатия в бою: кулдаун,
который игрок ни разу не нажал, тоже попадает в план. Если в логе есть таланты (CombatantInfo)
и доступен справочник талантов, кулдаун-талант учитывается, только если он взят.
Без данных о талантах учитываются «основные» кулдауны спека (их берут почти всегда) и те,
что игрок нажимал в этом бою.
"""
from __future__ import annotations

from collections import defaultdict

from . import game_data


def _norm(s) -> str:
    return "".join(ch for ch in str(s or "") if ch.isalnum()).lower()


def candidates(cls: str, spec: str) -> list[tuple]:
    """Рейдовые кулдауны спека из таблицы игровых данных: [(id, название, откат, основной)]."""
    c, s = _norm(cls), _norm(spec)
    return [(int(x["id"]), x["name"], float(x["cd"]), bool(x.get("core")), x.get("en") or x["name"])
            for x in game_data.raid_cds()
            if _norm(x.get("class")) == c and (not x.get("spec") or _norm(x["spec"]) == s)]


def _players(details: dict) -> list[dict]:
    out = []
    for role in ("tanks", "healers", "dps"):
        for p in (details or {}).get(role) or []:
            if not isinstance(p, dict) or p.get("id") is None:
                continue
            specs = p.get("specs") or []
            spec = specs[0].get("spec") if specs and isinstance(specs[0], dict) else (specs[0] if specs else "")
            out.append({"id": int(p["id"]), "name": p.get("name", ""), "cls": p.get("type", ""), "spec": spec,
                        "role": {"tanks": "Танк", "healers": "Лекарь", "dps": "DPS"}[role]})
    return out


def _taken_spells(combatant: list, talent_data, players: list[dict]) -> dict[int, tuple[set, set]] | None:
    """{игрок: (id способностей взятых талантов, id всех способностей дерева спека)}."""
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
        in_tree = {int(e[1]) for n in st["nodes"].values() for e in n["entries"].values() if e[1]}
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
        out[p["id"]] = (spells, in_tree)
    return out


def roster_cds(raw: dict, R: dict, talent_data=None) -> list[dict]:
    """Все рейдовые кулдауны состава с перезарядкой и источником (почему считаем, что он есть)."""
    players = _players(raw.get("details") or {})
    names = {int(a["gameID"]): a.get("name") for a in ((raw.get("report") or {}).get("masterData") or {}).get("abilities") or []
             if a.get("gameID") is not None}
    # Лог на английском — и не нажатые кулдауны называем по-английски, чтобы в плане не было смеси языков
    latin = sum(1 for n in names.values() if n and n.isascii()) > len(names) / 2
    pressed = defaultdict(list)
    for c in (R.get("extras") or {}).get("raid_cds") or []:
        if c.get("pid") is not None and c.get("id") is not None:
            pressed[(int(c["pid"]), int(c["id"]))].append(float(c["t"]))
    try:
        taken = _taken_spells(raw.get("combatant") or [], talent_data, players)
    except Exception:  # noqa: BLE001 — без талантов план строится по основным кулдаунам спеков
        taken = None
    dur = float((R.get("info") or {}).get("duration_s") or 0)
    out = []
    for p in players:
        tk = taken.get(p["id"]) if taken is not None else None
        for sid, name_ru, cd, core, name_en in candidates(p["cls"], p["spec"]):
            ts = sorted(pressed.get((p["id"], sid), []))
            if ts:
                source = f"нажимал в бою: {len(ts)}"
            elif tk is not None and sid in tk[1]:
                if sid not in tk[0]:
                    continue  # это талант, и он не взят
                source = "талант взят, в бою не нажимал"
            elif tk is not None:
                source = "базовая способность спека, в бою не нажимал"
            elif core:
                other = next((x.get("choice_with") for x in game_data.raid_cds() if int(x["id"]) == sid), None)
                if other and pressed.get((p["id"], int(other))):
                    continue  # нажимал второй вариант узла выбора
                source = "обычно есть у спека, в бою не нажимал"
            else:
                continue
            gaps = [b - a for a, b in zip(ts, ts[1:])]
            real_cd = round(min([cd] + [g for g in gaps if g > 20]) if gaps else cd, 1)
            out.append({"pid": p["id"], "player": p["name"], "role": p["role"], "cls": p["cls"], "spec": p["spec"],
                        "id": sid, "name": names.get(sid) or (name_en if latin else name_ru), "cd": real_cd, "used": len(ts),
                        "max_uses": int(dur // real_cd) + 1 if dur else None, "source": source})
    out.sort(key=lambda c: (c["role"] != "Лекарь", c["player"], -c["cd"]))
    return out

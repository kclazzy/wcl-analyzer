"""Лекари: лечение и оверхил, мана к концу боя, лечение в пики урона, внешние сейвы на игроков
и смерти, когда внешний сейв лекаря был свободен.

Данные: таблица лечения (уже есть), касты лекарей с маной и график лечения по времени — тем же
маленьким запросом, что бурсты и сейвы танков.
"""
from __future__ import annotations

from collections import defaultdict

from . import game_data
from .compare import _fmt_t

OOM = 0.05              # меньше 5% маны — «закончилась мана»
LOW_MANA_END = 0.10     # к концу килла меньше 10% — лекарь шёл на пределе
OVERHEAL_X = 1.5        # оверхил в полтора раза выше медианы лекарей — заметно
PEAK_PRE, PEAK_POST = 2.0, 8.0   # лечение «в пик» — от 2 с до начала окна пика до 8 с после
SAVE_WINDOW_S = 10.0    # внешний сейв «спас», если цель прожила ещё 10 с
READY_LOOKBACK_S = 6.0  # смерть «с откатанным внешним сейвом» — если сейв был откатан за 6 с до смерти


def _empty(reason: str) -> dict:
    return {"healers": [], "externals": [], "missed": [], "hints": [reason], "key": None}


def _unwrap_graph(g):
    if isinstance(g, dict) and "data" in g and isinstance(g["data"], dict) and "series" in g["data"]:
        return g["data"]
    return g if isinstance(g, dict) else {}


def heal_analysis(raw: dict, players: dict, owner, rel, nm, dur: float, spikes: list, deaths: list,
                  kill: bool = True, latin: bool = True) -> dict:
    healers = {pid: p for pid, p in players.items() if p.get("role") == "healer"}
    if not healers:
        return _empty("В этом бою нет лекарей")
    f0 = float((raw.get("fight") or {}).get("startTime") or 0)

    # ---------------------------------------------- лечение и оверхил
    ht = raw.get("heal_table") or {}
    ht = ht.get("data", ht) if isinstance(ht, dict) else {}
    ent = {int(e["id"]): e for e in (ht.get("entries") or []) if e.get("id") is not None}
    tot_heal = sum(float(ent.get(pid, {}).get("total") or 0) for pid in healers) or 1.0

    # ---------------------------------------------- мана по кастам
    mana: dict[int, list] = {}
    for pid, evs in (raw.get("heal_res") or {}).items():
        pts = []
        for ev in evs or []:
            for r in ev.get("classResources") or []:
                if int(r.get("type", -1)) == 0 and r.get("max"):
                    pts.append((rel(ev), float(r["amount"]) / float(r["max"])))
        if pts:
            mana[int(pid)] = sorted(pts)

    # ---------------------------------------------- лечение в пики (график по времени)
    g = _unwrap_graph(raw.get("heal_graph"))
    # в графике есть и строка «Total» (сумма) — только игроки, по числовому номеру
    series = {int(x["id"]): x for x in g.get("series") or [] if str(x.get("id", "")).lstrip("-").isdigit()}
    g0 = float(g.get("startTime") or f0)
    peak_ratio: dict[int, float] = {}
    win = [(sp["t"] - PEAK_PRE, sp["t"] + 5 + PEAK_POST) for sp in spikes or []]
    for pid in healers:
        x = series.get(pid)
        if not x or not win or not x.get("pointInterval"):
            continue
        step = float(x["pointInterval"]) / 1000
        ps = float(x.get("pointStart") or 0)
        # pointStart — время первой точки: либо от начала отчёта (как у боя), либо от начала графика
        start = (ps - f0) / 1000 if f0 and ps >= f0 - 1000 else (ps + g0 - f0) / 1000
        vals = [(start + i * step, float(v or 0)) for i, v in enumerate(x.get("data") or [])]
        vals = [(t, v) for t, v in vals if 0 <= t <= dur]
        if not vals:
            continue
        avg = sum(v for _, v in vals) / len(vals)
        inp = [v for t, v in vals if any(a <= t <= b for a, b in win)]
        if avg > 0 and inp:
            peak_ratio[pid] = (sum(inp) / len(inp)) / avg

    rows = []
    for pid, p in healers.items():
        e = ent.get(pid, {})
        total, over = float(e.get("total") or 0), float(e.get("overheal") or 0)
        m = mana.get(pid) or []
        oom = next((t for t, v in m if v < OOM), None)
        # сколько был без маны: до первого каста, где маны снова больше 10% (зелье, Озарение, реген), или до конца
        back = next((t for t, v in m if oom is not None and t > oom and v >= 2 * OOM), None) if oom is not None else None
        oom_s = ((back if back is not None else dur) - oom) if oom is not None else None
        rows.append({"pid": pid, "player": p["name"], "cls": p.get("cls"), "spec": p.get("spec"),
                     "healing": round(total), "hps": round(total / dur) if dur else 0, "share": round(total / tot_heal, 3),
                     "overheal": round(over / (total + over), 3) if total + over else None,
                     "mana_end": round(m[-1][1], 3) if m else None, "mana_min": round(min(v for _, v in m), 3) if m else None,
                     "oom_t": round(oom, 1) if oom is not None else None, "oom": _fmt_t(oom) if oom is not None else "",
                     "oom_s": round(oom_s) if oom_s is not None else None, "oom_to_end": oom is not None and back is None,
                     "peak": round(peak_ratio[pid], 2) if pid in peak_ratio else None,
                     "active": round(float(e["activeTime"]) / 1000 / dur, 3) if e.get("activeTime") and dur else None})
    rows.sort(key=lambda r: -r["healing"])

    # ---------------------------------------------- внешние сейвы лекарей на игроков
    ext = {int(c["id"]): c for c in game_data.tank_cds() if c.get("kind") == "external"}
    label = lambda c: (nm(int(c["id"])) if not str(nm(int(c["id"]))).startswith("Spell ") else  # noqa: E731
                       (c.get("en") if latin else c.get("name")) or c.get("en"))
    died_at = defaultdict(list)
    for d in deaths or []:
        died_at[int(d["id"])].append(float(d["t"]))
    uses = []
    presses = defaultdict(list)   # (лекарь, сейв) → времена
    for ev in raw.get("casts") or []:
        if ev.get("type") != "cast":
            continue
        ab = int(ev.get("abilityGameID", 0))
        src = owner(int(ev.get("sourceID", -1)))
        if ab not in ext or src not in healers:
            continue
        t, tgt = rel(ev), int(ev.get("targetID", -1))
        presses[(src, ab)].append(t)
        if tgt in players:
            dead = next((x for x in died_at.get(tgt, []) if t <= x <= t + SAVE_WINDOW_S), None)
            uses.append({"t": round(t, 1), "time": _fmt_t(t), "healer": healers[src]["name"], "cls": healers[src].get("cls"),
                         "name": label(ext[ab]), "id": ab, "target": players[tgt]["name"],
                         "target_role": players[tgt].get("role"), "died": dead is not None})
    uses.sort(key=lambda u: u["t"])

    # ---------------------------------------------- смерть, когда внешний сейв лекаря был откатан
    owned = defaultdict(set)   # у кого из лекарей какой внешний сейв есть
    for pid, p in healers.items():
        for ab, c in ext.items():
            if c.get("plan", True) is False:
                continue
            if (c.get("class") or "").lower() == (p.get("cls") or "").lower() and \
                    (not c.get("spec") or (c["spec"] or "").lower() == (p.get("spec") or "").lower()):
                owned[pid].add(ab)
    for (pid, ab) in presses:
        owned[pid].add(ab)

    from .raid_tank import _school_ok
    school_by_name = {}
    for a in ((raw.get("report") or {}).get("masterData") or {}).get("abilities") or []:
        try:
            school_by_name.setdefault(a.get("name"), int(a.get("type") or 0))
        except (TypeError, ValueError):
            pass

    def ready(pid, ab, t):
        cd = float(ext[ab]["cd"])
        return all(not (x <= t < x + cd) for x in presses.get((pid, ab), []))

    def alive(pid, t):
        return not any(x <= t for x in died_at.get(pid, []))
    missed = []
    for d in deaths or []:
        if d.get("wipe_tail"):   # конец вайпа — не в счёт
            continue
        t = float(d["t"])
        # только смерть в пик урона по рейду (или танка): от луж и избегаемых механик внешний сейв не нужен
        tank = players.get(int(d["id"]), {}).get("role") == "tank"
        if not tank and not any(a <= t <= b for a, b in win):
            continue
        sch = school_by_name.get(d.get("ability"), 0)   # Blessing of Spellwarding — только от магии
        free = [(pid, ab) for pid, abs_ in owned.items() for ab in abs_
                if pid != int(d["id"]) and alive(pid, t) and _school_ok(ext[ab], sch)
                and ready(pid, ab, t - READY_LOOKBACK_S) and ready(pid, ab, t)]
        if free:
            missed.append({"t": round(t, 1), "time": d.get("time") or _fmt_t(t), "player": d["player"],
                           "ability": d.get("ability") or "", "free": [f"{label(ext[ab])} ({healers[pid]['name']})" for pid, ab in free[:2]]})

    H = {"healers": rows, "externals": uses, "missed": missed, "spikes": len(spikes or []),
         "has_mana": bool(mana), "has_graph": bool(peak_ratio)}
    H["hints"], H["key"] = _hints(H, dur, kill)
    for r in rows:
        r.pop("pid", None)
    return H


def _hints(H: dict, dur: float, kill: bool) -> tuple[list[str], str | None]:
    out, key = [], None
    rows = H["healers"]
    ooms = [r for r in rows if r["oom_t"] is not None and r["oom_t"] < dur - 5 and (r["oom_s"] or 0) >= 5]
    for r in ooms:
        out.append(f"{r['player']}: закончилась мана в {r['oom']} — " +
                   (f"до конца боя {_fmt_t(r['oom_s'])} без маны" if r["oom_to_end"] else f"без маны {r['oom_s']} с"))
    if ooms:
        key = "Лекари: закончилась мана — " + ", ".join(f"{r['player']} ({r['oom']})" for r in ooms[:2]) \
            + " (подробно — во вкладке «Лекари»)"
    low = [r for r in rows if r not in ooms and r["mana_end"] is not None and r["mana_end"] < LOW_MANA_END and kill]
    if low:
        out.append("К концу килла почти без маны: " + ", ".join(f"{r['player']} ({r['mana_end']:.0%})" for r in low))
    ovs = sorted(r["overheal"] for r in rows if r["overheal"] is not None)
    if len(ovs) >= 3:
        med = ovs[len(ovs) // 2]
        hi = [r for r in rows if r["overheal"] is not None and med > 0 and r["overheal"] >= OVERHEAL_X * med and r["overheal"] >= 0.3]
        for r in hi:
            out.append(f"{r['player']}: оверхил {r['overheal']:.0%} — заметно выше, чем у остальных лекарей ({med:.0%})")
    pk = [r for r in rows if r["peak"] is not None]
    if len(pk) >= 2:
        weak = [r for r in pk if r["peak"] < 1.0]
        if weak:
            out.append("В пики урона лечили меньше обычного: " + ", ".join(f"{r['player']} (×{r['peak']:.1f} от среднего)" for r in weak)
                       + " — кулдауны и сильное лечение лучше беречь под пики")
    for m in H["missed"][:3]:
        out.append(f"{m['time']} смерть в пик урона: {m['player']} («{m['ability']}»), а внешний сейв был свободен: {', '.join(m['free'])}")
    if H["missed"] and not key:
        m = H["missed"][0]
        key = (f"Лекари: {m['time']} смерть в пик урона — {m['player']}, а {m['free'][0]} был свободен "
               "(подробно — во вкладке «Лекари»)")
    saved = [u for u in H["externals"] if not u["died"]]
    if H["externals"]:
        out.append(f"Внешних сейвов на игроков: {len(H['externals'])}, цель выжила — {len(saved)}")
    if not out:
        out.append("Замечаний по лекарям нет")
    return out, key

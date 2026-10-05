"""Танки: танкбастеры (крупные удары босса по танку) и чем танк был прикрыт в момент удара — своим защитным
кулдауном или внешним сейвом от лекаря. Голый удар, смерть от удара, откатанный, но не нажатый кулдаун;
сравнение с лучшими киллами и заметка MRT для танков.

Танкбастер наверняка — если механика есть в гайде Mythic Trap с типом «Tankbuster» / «Tank Mechanic» и т. п.;
иначе — способность босса, урон от которой почти весь по танкам (не ближний бой), с ударами в несколько раз
крупнее обычной атаки босса. Кулдауны — game_data.json → tank_cds (номера проверены на Wowhead).
Активная защита (Ironfur, Shield Block и т. п.) висит почти всё время и здесь не оценивается.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from statistics import median

from . import game_data
from .compare import _fmt_t
from .raid_burst import _dead_at, _dead_spans, _fits, _key, _phase_label

CLUSTER_S = 3.0        # удары одной способности ближе 3 с — одно применение
DEATH_S = 4.0          # танк умер в первые 4 с после удара — «умер от удара»
BUSTER_X = 4.0         # удар в 4 раза крупнее обычной атаки босса — танкбастер
MAJOR_CD = 60          # «был откатан» — только кулдауны от минуты
MAX_BUSTERS = 5
GUIDE_TYPES = {"tankbuster", "tankmechanic", "tankcombo", "tankbustersolo", "tankbustersoak", "tankbolt",
               "tankdebuff", "tankswap", "tanksoaks"}
MELEE_RE = re.compile(r"^(melee|ближний бой|атака ближнего боя|auto attack|автоатака)$", re.I)


def _table() -> dict[int, dict]:
    return {int(c["id"]): c for c in game_data.tank_cds()}


def _guide_busters(boss: str | None) -> list[dict]:
    if not boss:
        return []
    out = []
    try:
        from .guides import bosses
        for _r, b in bosses():
            if _key(b.get("name")) != _key(boss):
                continue
            for lst in (b.get("abilities") or {}).values():
                for a in lst or []:
                    if _key(a.get("type")) in GUIDE_TYPES:
                        out.append({"id": a.get("id"), "name": _key(a.get("name"))})
    except Exception:  # noqa: BLE001 — гайды необязательны
        pass
    return out


def tank_analysis(raw: dict, players: dict, owner, rel, nm, dur: float, phases: list[dict],
                  deaths: list[dict] | None = None) -> dict:
    """{busters: [{id, name, n, covered}], events: [удар…], cds: [кулдауны танков…], hints, key, mrt}."""
    table = _table()
    tanks = {pid: p for pid, p in players.items() if p.get("role") == "tank"}
    if not tanks:
        return {"busters": [], "events": [], "cds": [], "hints": [], "key": None, "mrt": [], "tanks": []}
    dead = _dead_spans(raw, players, rel, dur)

    # ---------------------------------------------- удары по танкам
    hits_all: dict[int, list] = defaultdict(list)       # способность → [(t, игрок, урон, без снижения)]
    tot_by_ab, tank_by_ab = Counter(), Counter()
    melee_hits = []
    for ev in raw.get("taken") or []:
        if ev.get("type") != "damage":
            continue
        tgt = int(ev.get("targetID", -1))
        if tgt not in players or owner(int(ev.get("sourceID", -1))) in players:
            continue
        a = float(ev.get("amount", 0) or 0) + float(ev.get("absorbed", 0) or 0)
        if a <= 0:
            continue
        ab = int(ev.get("abilityGameID", 0))
        tot_by_ab[ab] += a
        if tgt in tanks:
            tank_by_ab[ab] += a
            if ab == 1 or MELEE_RE.match(nm(ab) or ""):
                melee_hits.append(a)
                continue
            un = ev.get("unmitigatedAmount")
            hits_all[ab].append((rel(ev), tgt, a, float(un) if un else None))
    base = median(melee_hits) if melee_hits else (median([h[2] for v in hits_all.values() for h in v]) if hits_all else 0)
    guide = _guide_busters(((raw.get("fight") or {}).get("name")))

    busters = []
    for ab, hs in hits_all.items():
        if tot_by_ab[ab] <= 0 or tank_by_ab[ab] / tot_by_ab[ab] < 0.7:
            continue
        clusters = []
        for h in sorted(hs):
            if clusters and h[0] - clusters[-1][-1][0] <= CLUSTER_S:
                clusters[-1].append(h)
            else:
                clusters.append([h])
        occ = []
        for c in clusters:
            per = Counter()
            for _t, pid, a, _u in c:
                per[pid] += a
            pid, dmg = per.most_common(1)[0]
            uns = [u for _t, p2, _a, u in c if p2 == pid]
            un = sum(u for u in uns if u) if all(uns) else None
            occ.append({"t": round(c[0][0], 1), "pid": pid, "damage": round(dmg), "unmitigated": round(un) if un else None})
        name = nm(ab)
        sure = any((g["id"] and int(g["id"]) == ab) or (g["name"] and g["name"] == _key(name)) for g in guide)
        big = base > 0 and median(o["damage"] for o in occ) >= BUSTER_X * base
        if not (sure or (big and len(occ) <= max(1, dur / 8))):
            continue
        busters.append({"id": ab, "name": name, "sure": sure, "occ": occ, "total": tank_by_ab[ab]})
    busters = sorted(busters, key=lambda b: -b["total"])[:MAX_BUSTERS]

    # ---------------------------------------------- защитные кулдауны
    presses: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))   # игрок → кулдаун → времена
    ext_casts = []                                                                           # (t, кто, кому, id)
    for ev in raw.get("casts") or []:
        if ev.get("type") != "cast":
            continue
        ab = int(ev.get("abilityGameID", 0))
        c = table.get(ab)
        src = owner(int(ev.get("sourceID", -1)))
        if not c or src not in players or not _fits(c, players[src]):
            continue
        t = round(rel(ev), 1)
        presses[src][ab].append(t)
        if c["kind"] == "external":
            ext_casts.append((t, src, int(ev.get("targetID", -1)), ab))
    by_buff = {int(c["buff"]): c for c in table.values() if c.get("buff")}
    spans: dict[int, list] = defaultdict(list)       # танк → [(начало, конец, id кулдауна, кто)]
    open_: dict = {}
    for ev in sorted(raw.get("tank_buffs") or [], key=lambda e: e.get("timestamp", 0)):
        c = by_buff.get(int(ev.get("abilityGameID", 0)))
        tgt = int(ev.get("targetID", -1))
        if not c or tgt not in tanks:
            continue
        src = owner(int(ev.get("sourceID", -1)))
        key = (tgt, int(c["id"]))
        if ev.get("type") == "applybuff":
            open_[key] = (rel(ev), src)
        elif ev.get("type") == "removebuff":
            t0, s0 = open_.pop(key, (0.0, src))
            spans[tgt].append((t0, rel(ev), int(c["id"]), s0))
    for (tgt, cid), (t0, s0) in open_.items():
        spans[tgt].append((t0, dur, cid, s0))
    for pid, per in presses.items():   # по касту: от нажатия на время действия (если баффа в логе нет)
        for cid, ts in per.items():
            c = table[cid]
            if c["kind"] == "self" and pid in tanks:
                spans[pid] += [(t, t + float(c["dur"]), cid, pid) for t in ts]
    for t, src, tgt, cid in ext_casts:
        if tgt in tanks:
            spans[tgt].append((t, t + float(table[cid]["dur"]), cid, src))

    def has_cd(pid: int, c: dict) -> bool:
        return _fits(c, players[pid]) and (c.get("base") or int(c["id"]) in presses.get(pid, {}))

    def ready(pid: int, c: dict, t: float) -> bool:
        ts = presses.get(pid, {}).get(int(c["id"]), [])
        return sum(1 for x in ts if t - float(c["cd"]) < x <= t) < int(c.get("charges") or 1)

    healers = [pid for pid, p in players.items() if p.get("role") == "healer"]
    events = []
    for b in busters:
        for k, o in enumerate(b["occ"]):
            t, pid = o["t"], o["pid"]
            on = [s for s in spans.get(pid, []) if s[0] - 0.3 <= t <= s[1] + 0.3]
            own = list(dict.fromkeys(s[2] for s in on if s[3] == pid))
            ext = list(dict.fromkeys((s[2], s[3]) for s in on if s[3] != pid))
            died = any(d.get("id") == pid and t <= d["t"] <= t + DEATH_S for d in deaths or [])
            alive = not _dead_at(dead, pid, t)
            free_own = [c["id"] for c in table.values() if c["kind"] == "self" and alive and has_cd(pid, c)
                        and float(c["cd"]) >= MAJOR_CD and int(c["id"]) not in own and ready(pid, c, t)]
            free_ext = [(c["id"], h) for h in healers for c in table.values()
                        if c["kind"] == "external" and has_cd(h, c) and not _dead_at(dead, h, t) and ready(h, c, t)
                        and (c["id"], h) not in ext]
            covered = bool(own or ext)
            mit = (1 - o["damage"] / o["unmitigated"]) if o.get("unmitigated") else None
            n, label = _phase_label(phases, t)
            events.append({
                "t": t, "time": _fmt_t(t), "phase": n, "phase_label": label, "id": b["id"], "ability": b["name"], "k": k,
                "tank": players[pid]["name"], "pid": pid, "cls": players[pid].get("cls"),
                "damage": o["damage"], "mitigated": round(mit, 3) if mit is not None else None,
                "own": [{"id": i, "name": nm(i) if not nm(i).startswith("Spell ") else table[i]["name"]} for i in own],
                "ext": [{"id": i, "name": nm(i) if not nm(i).startswith("Spell ") else table[i]["name"],
                         "from": players[h]["name"] if h in players else "—"} for i, h in ext],
                "covered": covered, "died": died,
                "ready": [nm(i) if not nm(i).startswith("Spell ") else table[i]["name"] for i in free_own],
                "ready_ext": [f"{table[i]['name']} ({players[h]['name']})" for i, h in free_ext][:3],
                "ready_ids": free_own, "ready_ext_ids": [[i, h] for i, h in free_ext]})
    events.sort(key=lambda e: e["t"])
    for b in busters:
        evs = [e for e in events if e["id"] == b["id"]]
        b.update({"n": len(evs), "covered": sum(1 for e in evs if e["covered"]),
                  "died": sum(1 for e in evs if e["died"])})
        b.pop("occ", None)

    # ---------------------------------------------- кулдауны танков: когда нажаты, сколько раз, на удары ли
    hit_t = defaultdict(list)
    for e in events:
        hit_t[e["pid"]].append(e["t"])
    cds = []
    for pid in tanks:
        alive_s = max(0.0, dur - sum(b2 - a2 for a2, b2 in dead.get(pid, [])))
        for cid, ts in sorted(presses.get(pid, {}).items(), key=lambda x: min(x[1])):
            c = table[cid]
            if c["kind"] != "self":
                continue
            ts = sorted(ts)
            on_hit = [t for t in ts if any(t - 1 <= h <= t + float(c["dur"]) + 0.5 for h in hit_t[pid])]
            cds.append({"tank": players[pid]["name"], "cls": players[pid].get("cls"), "spec": players[pid].get("spec"),
                        "id": cid, "name": nm(cid) if not nm(cid).startswith("Spell ") else c["name"],
                        "cd": c["cd"], "times": ts, "time": ", ".join(_fmt_t(x) for x in ts), "used": len(ts),
                        "max_uses": max(len(ts), int(c.get("charges") or 1) + int(alive_s // float(c["cd"]))),
                        "on_hit": len(on_hit)})
    for pid in tanks:   # танк, не нажавший ни одного кулдауна из таблицы
        if not any(c["tank"] == players[pid]["name"] for c in cds):
            cds.append({"tank": players[pid]["name"], "cls": players[pid].get("cls"), "spec": players[pid].get("spec"),
                        "id": None, "name": "", "cd": None, "times": [], "time": "", "used": 0, "max_uses": None,
                        "on_hit": 0})
    T = {"busters": busters, "events": events, "cds": cds, "tanks": [p["name"] for p in tanks.values()],
         "duration": round(dur, 1), "top": None,
         "light": [{"id": e["id"], "k": e["k"], "covered": e["covered"], "cds": [x["id"] for x in e["own"] + e["ext"]],
                    "cls": e["cls"]} for e in events]}
    plan_tank(T, players, phases, presses_free=None)
    refresh(T)
    return T


def plan_tank(T: dict, players: dict, phases: list[dict], presses_free=None) -> None:
    """План на следующий пулл: на каждый танкбастер — свой кулдаун танка или внешний сейв лекаря, с учётом
    отката. Сначала — то, что сработало в этом бою, затем — что жмут лучшие киллы, затем — любой свободный."""
    from .raid_top import CLASS_COLOR, mrt_line
    from .raid import phase_at
    table = _table()
    top_cds = ((T.get("top") or {}).get("cds") or {})
    used: dict = defaultdict(list)        # (игрок, кулдаун) → запланированные времена
    healers = [pid for pid, p in players.items() if p.get("role") == "healer"]
    rows = []

    def free(pid, cid, t):
        c = table[cid]
        return sum(1 for x in used[(pid, cid)] if t - float(c["cd"]) < x <= t + float(c["cd"])) < int(c.get("charges") or 1)

    def own_has(pid, c):
        return (_fits(c, players[pid]) and c["kind"] == "self" and float(c["cd"]) >= MAJOR_CD
                and (c.get("base") or any(x["id"] == c["id"] for e in T["events"] if e["pid"] == pid for x in e["own"])
                     or int(c["id"]) in [i for e in T["events"] if e["pid"] == pid for i in e["ready_ids"]]))

    for e in T["events"]:
        pid, t = e["pid"], e["t"]
        if pid not in players:
            continue
        pref = [x["id"] for x in e["own"]] + [int(i) for i in (top_cds.get(str(e["id"])) or [])]
        own = [c for c in table.values() if own_has(pid, c)]
        own.sort(key=lambda c: (pref.index(int(c["id"])) if int(c["id"]) in pref else 99, -float(c["cd"])))
        pick, why = None, ""
        for c in own:
            if free(pid, int(c["id"]), t):
                pick, why = (pid, int(c["id"])), ("как в бою" if int(c["id"]) in [x["id"] for x in e["own"]]
                                                  else "как у топа" if int(c["id"]) in pref else "свободный")
                break
        if pick is None:
            exts = [(int(x["id"]), next((h for h in healers if players[h]["name"] == x["from"]), None)) for x in e["ext"]]
            exts += [(c["id"], h) for h in healers for c in table.values()
                     if c["kind"] == "external" and _fits(c, players[h]) and c.get("base")]
            for cid, h in exts:
                if h is not None and free(h, cid, t):
                    pick, why = (h, cid), "внешний"
                    break
        row = {"t": t, "time": e["time"], "phase_label": e["phase_label"], "ability": e["ability"], "id": e["id"],
               "tank": e["tank"], "pick": None, "why": why, "mrt": ""}
        if pick:
            who, cid = pick
            used[(who, cid)].append(t)
            row["pick"] = {"player": players[who]["name"], "cls": players[who].get("cls"), "id": cid,
                           "name": table[cid]["name"], "external": who != pid}
            ph = phase_at(phases, t)
            n = ph["n"] if ph and (ph.get("n") or 0) > 1 else None
            line = mrt_line(t, "«" + e["ability"] + "»", [row["pick"]], n, round(t - ph["t"], 1) if n else None,
                            mech_id=e["id"])
            if who != pid:
                color = CLASS_COLOR.get(e.get("cls") or "")
                line += " → " + (f"|cff{color}{e['tank']}|r" if color else e["tank"])
            row["mrt"] = line
        rows.append(row)
    T["plan"] = rows
    T["mrt"] = "\n".join(["Танки (WCL Analyzer)"] + [r["mrt"] for r in rows if r["mrt"]]) if any(r["mrt"] for r in rows) else ""


def _list(xs, n=4) -> str:
    return ", ".join(xs[:n]) + ("…" if len(xs) > n else "")


def refresh(T: dict) -> None:
    """Подсказки и главная строка для «Главного по бою» (T["key"])."""
    out, keys = [], {}
    events, busters, top = T.get("events") or [], T.get("busters") or [], T.get("top") or {}
    if not busters:
        T["hints"] = ["Танкбастеров не найдено: нет крупных ударов босса по танкам (кроме ближнего боя)"] if T.get("tanks") else []
        T["key"] = None
        return
    for b in busters[:3]:
        s = f"«{b['name']}»: ударов по танкам — {b['n']}, прикрыто кулдауном — {b['covered']}"
        tb = (top.get("abilities") or {}).get(str(b["id"]))
        if tb:
            s += f"; у лучших киллов — {tb['share']:.0%}" + (f", чаще всего: {', '.join(tb['names'][:3])}" if tb.get("names") else "")
            if b["n"] and b["covered"] / b["n"] < tb["share"] - 0.3:
                keys.setdefault("top", f"Танки: «{b['name']}» прикрыт кулдауном в {b['covered']} из {b['n']} ударов, "
                                       f"у лучших киллов — в {tb['share']:.0%}")
        out.append(s)
    for e in [e for e in events if e["died"]][:3]:
        s = (f"Танк {e['tank']} умер от «{e['ability']}» в {e['time']}"
             + (f" ({e['phase_label']})" if e["phase_label"] else "")
             + (f" — без кулдауна, хотя был готов: {_list(e['ready'])}" if not e["covered"] and e["ready"]
                else " — без кулдауна" if not e["covered"] else " — несмотря на кулдаун")
             + (f"; внешние сейвы были свободны: {_list(e['ready_ext'], 2)}" if not e["covered"] and e["ready_ext"] else ""))
        out.append(s)
        keys.setdefault("death", s)
    bare = [e for e in events if not e["covered"] and not e["died"] and e["ready"]]
    if bare:
        s = "Удар без кулдауна, хотя кулдаун был откатан: " + "; ".join(
            f"{e['time']} «{e['ability']}», {e['tank']} — {_list(e['ready'], 2)}" for e in bare[:3]) + ("…" if len(bare) > 3 else "")
        out.append(s)
        if len(bare) >= 2:
            keys.setdefault("bare", s)
    T["hints"] = out
    T["key"] = keys.get("death") or keys.get("top") or keys.get("bare")


def top_summary(kills: list[dict], table: dict | None = None) -> dict | None:
    """Лучшие киллы: какая доля ударов каждой способности прикрыта и какими кулдаунами чаще всего."""
    ks = [k for k in kills or [] if k.get("tank")]
    if not ks:
        return None
    table = table or _table()
    per = defaultdict(list)
    names = defaultdict(Counter)
    for k in ks:
        for e in k["tank"]:
            per[str(e["id"])].append(bool(e["covered"]))
            for cid in e.get("cds") or []:
                names[str(e["id"])][int(cid)] += 1
    abilities = {ab: {"share": round(sum(v) / len(v), 2), "n": len(v),
                      "names": [table[c]["name"] for c, _ in names[ab].most_common(4) if c in table]}
                 for ab, v in per.items() if v}
    return {"kills": len(ks), "abilities": abilities,
            "cds": {ab: [c for c, _ in names[ab].most_common(6)] for ab in names}}

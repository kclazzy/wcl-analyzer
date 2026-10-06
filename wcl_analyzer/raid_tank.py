"""Танки: танкбастеры (крупные удары босса по танку) и чем танк был прикрыт в момент удара — своим защитным
кулдауном или внешним сейвом. Голый удар, смерть от удара, откатанный, но не нажатый кулдаун; сравнение
с лучшими киллами и заметка MRT для танков.

Танкбастер — способность босса (не аддов), урон от которой почти весь по танкам (не ближний бой), с ударами
в несколько раз крупнее ближнего боя босса; сравнение — по урону до защиты (unmitigatedAmount), чтобы удар,
который все прикрывают кулдауном, не выпадал. Механики из гайда Mythic Trap с типом «Tankbuster» и т. п. —
тоже бастеры, если удар хотя бы вдвое крупнее ближнего боя. Кулдауны — game_data.json → tank_cds.
Активная защита (Ironfur, Shield Block и т. п.) висит почти всё время и здесь не оценивается.
Таблица кулдаунов — по основной игре: на Classic и других версиях «был готов» и план не считаются.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from statistics import median

from . import game_data
from .compare import _fmt_t
from .raid_burst import _dead_at, _dead_spans, _fits, _key, _phase_label

CLUSTER_S = 3.0        # удары одной способности по танку в первые 3 с от первого — одно применение (комбо)
DEATH_S = 4.0          # танк умер в первые 4 с после удара — «умер от удара»
BUSTER_X = 4.0         # самый крупный удар в 4 раза больше ближнего боя босса — танкбастер
GUIDE_X = 0.7          # механика из гайда — серия ударов не слабее удара ближнего боя (и не чаще раза в 8 с)
MIN_MELEE = 20         # меньше 20 ударов ближнего боя босса — база по всем ударам босса по танкам
MAJOR_CD = 60          # «был откатан» — только кулдауны от минуты
PRESS_LEAD_S = 1.5     # в заметке MRT — нажать за 1,5 с до удара
MAX_BUSTERS = 5
GUIDE_TYPES = {"tankbuster", "tankmechanic", "tankcombo", "tankbustersolo", "tankbustersoak", "tankbolt",
               "tankdebuff", "tankswap", "tanksoaks"}
last_candidates: list = []   # кандидаты в танкбастеры последнего разбора — для проверки порогов
MELEE_RE = re.compile(r"^(melee|ближний бой|атака ближнего боя|auto attack|автоатака)$", re.I)


def _table() -> dict[int, dict]:
    return {int(c["id"]): c for c in game_data.tank_cds()}


def _grp(c: dict) -> int:
    """Общий откат: два номера одной способности (Защитник древних королей) — один кулдаун."""
    return int(c.get("group") or c["id"])


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


def _school(raw: dict) -> dict[int, int]:
    """Школа урона способности из masterData (1 — физический, остальное — магия)."""
    out = {}
    for a in ((raw.get("report") or {}).get("masterData") or {}).get("abilities") or []:
        try:
            out[int(a["gameID"])] = int(a.get("type") or 0)
        except (TypeError, ValueError, KeyError):
            pass
    return out


def _school_ok(c: dict, sch: int) -> bool:
    want = c.get("school")
    if not want or not sch:
        return True
    magic = bool(sch & ~1)
    return magic if want == "magic" else (sch & 1) == 1


def _empty(site: str) -> dict:
    return {"busters": [], "events": [], "cds": [], "hints": [], "key": None, "mrt": "", "plan": [], "tanks": [],
            "light": [], "site": site, "top": None}


def tank_analysis(raw: dict, players: dict, owner, rel, nm, dur: float, phases: list[dict],
                  deaths: list[dict] | None = None) -> dict:
    """{busters, events: [удар по танку…], cds: [кулдауны танков…], plan, mrt, hints, key, light (для топа)}."""
    table = _table()
    site = raw.get("site") or "www"
    tanks = {pid: p for pid, p in players.items() if p.get("role") == "tank"}
    if not tanks:
        return _empty(site)
    dead = _dead_spans(raw, players, rel, dur)
    schools = _school(raw)
    actors = ((raw.get("report") or {}).get("masterData") or {}).get("actors") or []
    boss_ids = {int(a["id"]) for a in actors if a.get("subType") == "Boss"}
    from_boss = (lambda src: src in boss_ids) if boss_ids else (lambda src: True)

    # ---------------------------------------------- удары по танкам
    hits: dict[int, list] = defaultdict(list)          # способность → [(t, танк, урон, без снижения, поглощено, от босса)]
    tot_by_ab, tank_by_ab, boss_by_ab = Counter(), Counter(), Counter()
    melee_un, melee_a, boss_tank_hits = [], [], []
    for ev in raw.get("taken") or []:
        if ev.get("type") != "damage":
            continue
        tgt, src = int(ev.get("targetID", -1)), int(ev.get("sourceID", -1))
        if tgt not in players or owner(src) in players:
            continue
        absorbed = float(ev.get("absorbed", 0) or 0)
        a = float(ev.get("amount", 0) or 0) + absorbed
        if a <= 0:
            continue
        ab = int(ev.get("abilityGameID", 0))
        tot_by_ab[ab] += a
        if tgt not in tanks:
            continue
        tank_by_ab[ab] += a
        un = float(ev.get("unmitigatedAmount") or 0) or None
        boss = from_boss(src)
        if boss:
            boss_by_ab[ab] += a
            boss_tank_hits.append(un or a)
        if ab == 1 or MELEE_RE.match(nm(ab) or ""):
            if boss:
                melee_a.append(a)
                if un:
                    melee_un.append(un)
            continue
        hits[ab].append((rel(ev), tgt, a, un, absorbed, boss))
    use_un = len(melee_un) >= MIN_MELEE
    melee = melee_un if use_un else melee_a
    if len(melee) >= MIN_MELEE:
        base = median(melee)
    else:   # у босса нет ближнего боя (касты, совет боссов): четверть всех ударов босса по танкам
        vals = sorted(boss_tank_hits)
        base = vals[len(vals) // 4] if vals else 0.0
    guide = _guide_busters(((raw.get("fight") or {}).get("name")))

    busters = []
    last_candidates.clear()
    last_candidates.append({"base": round(base), "use_un": use_un, "melee_n": len(melee), "boss_ids": len(boss_ids)})
    for ab, hs in hits.items():
        if tot_by_ab[ab] <= 0 or tank_by_ab[ab] / tot_by_ab[ab] < 0.7:
            continue
        name = nm(ab)
        sure = any((g["id"] and int(g["id"]) == ab) or (g["name"] and g["name"] == _key(name)) for g in guide)
        if not sure and boss_by_ab[ab] < 0.5 * tank_by_ab[ab]:
            continue   # удары аддов — не танкбастеры (кроме механик из гайда)
        occ = []
        by_tank = defaultdict(list)
        for h in sorted(hs):
            by_tank[h[1]].append(h)
        for pid, lst in by_tank.items():   # по каждому танку отдельно: удар по двум танкам — две строки
            cl: list[list] = []
            for h in lst:
                if cl and h[0] - cl[-1][0][0] <= CLUSTER_S:   # от первого удара, а не от предыдущего: тики не склеиваются
                    cl[-1].append(h)
                else:
                    cl.append([h])
            for c in cl:
                size = max((x[3] if use_un and x[3] else x[2]) for x in c)
                series = sum((x[3] if use_un and x[3] else x[2]) for x in c)   # серия ударов механики целиком
                dmg = sum(x[2] for x in c)
                un = sum(x[3] for x in c) if all(x[3] for x in c) else None
                occ.append({"t": round(c[0][0], 1), "t_end": round(c[-1][0], 1), "pid": pid, "damage": round(dmg),
                            "unmitigated": round(un) if un else None, "absorbed": round(sum(x[4] for x in c)),
                            "size": size, "series": series, "hits": len(c)})
        med = median(o["size"] for o in occ)
        if sure:
            # механика из гайда: серия ударов не слабее одного удара ближнего боя и не чаще раза в 8 с —
            # на эпохальном танкбастер бьёт в 1–2 удара ближнего боя, и порог «вдвое больше» его пропускал
            # (проверено на настоящем логе); частые тики (доты) отсекает частота
            med = median(o["series"] for o in occ)
            ok = len(occ) <= max(2, dur / 8) and (not base or med >= GUIDE_X * base)
        else:
            ok = bool(base) and med >= BUSTER_X * base and len(occ) <= max(1, dur / 8)
        last_candidates.append({"name": name, "sure": sure, "ratio": round(med / base, 2) if base else None,
                                "n": len(occ), "ok": ok, "total": round(tank_by_ab[ab]), "boss": boss_by_ab[ab] >= 0.5 * tank_by_ab[ab]})
        if ok:
            busters.append({"id": ab, "name": name, "sure": sure, "occ": occ, "total": tank_by_ab[ab],
                            "school": schools.get(ab, 0)})
    busters = sorted(busters, key=lambda b: -b["total"])[:MAX_BUSTERS]

    # ---------------------------------------------- защитные кулдауны: нажатия, откат и заряды по факту
    presses: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))   # игрок → группа → времена
    ext_casts = []
    for ev in raw.get("casts") or []:
        if ev.get("type") != "cast":
            continue
        c = table.get(int(ev.get("abilityGameID", 0)))
        src = owner(int(ev.get("sourceID", -1)))
        if not c or src not in players or not _fits(c, players[src]):
            continue
        t = round(rel(ev), 1)
        presses[src][_grp(c)].append(t)
        if c["kind"] == "external":
            ext_casts.append((t, src, int(ev.get("targetID", -1)), _grp(c)))
    for per in presses.values():
        for v in per.values():
            v.sort()
    base_of = {}
    for c in table.values():   # запись группы — та, что без group (основной номер)
        if not c.get("group") or _grp(c) not in base_of:
            base_of[_grp(c)] = c

    def eff(pid: int, g: int) -> tuple[float, int]:
        """Откат и заряды у этого игрока: по таблице, а если в бою нажимал чаще — по факту (таланты)."""
        c = base_of[g]
        cd, ch = float(c["cd"]), int(c.get("charges") or 1)
        ts = presses.get(pid, {}).get(g, [])
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        if gaps and min(gaps) < 0.5 * cd:
            ch = max(ch, 2)
        elif gaps and min(gaps) < cd:
            cd = max(10.0, min(gaps) - 1)
        return cd, ch

    by_buff = {}
    for c in table.values():
        if c.get("buff"):
            by_buff[int(c["buff"])] = c
    spans: dict[int, list] = defaultdict(list)       # танк → [(начало, конец, группа, кто)]
    open_: dict = {}
    for ev in sorted(raw.get("tank_buffs") or [], key=lambda e: e.get("timestamp", 0)):
        c = by_buff.get(int(ev.get("abilityGameID", 0)))
        tgt = int(ev.get("targetID", -1))
        if not c or tgt not in tanks:
            continue
        src = owner(int(ev.get("sourceID", -1)))
        key = (tgt, _grp(c), src)
        t = rel(ev)
        if ev.get("type") == "applybuff":
            open_.setdefault(key, t)
        elif ev.get("type") == "removebuff":
            # снят без наложения: наложен до начала записи — не раньше, чем за время действия
            t0 = open_.pop(key, max(0.0, t - float(c["dur"])))
            spans[tgt].append((t0, t, _grp(c), src))
    for (tgt, g, src), t0 in open_.items():
        spans[tgt].append((t0, min(dur, t0 + 3 * float(base_of[g]["dur"])), g, src))
    debuff_of = {int(x): c for c in table.values() for x in c.get("boss_debuff") or []}
    open_d: dict = {}
    for ev in sorted(raw.get("boss_debuffs") or [], key=lambda e: e.get("timestamp", 0)):
        c = debuff_of.get(int(ev.get("abilityGameID", 0)))
        src = owner(int(ev.get("sourceID", -1)))
        if not c or src not in tanks:
            continue
        key, t = (src, int(ev.get("targetID", -1))), rel(ev)
        if ev.get("type") == "applydebuff":
            open_d.setdefault(key, t)
        elif ev.get("type") == "removedebuff":
            spans[src].append((open_d.pop(key, max(0.0, t - float(c["dur"]))), t, _grp(c), src))
    for pid, per in presses.items():   # по касту: от нажатия на время действия (если баффа в логе нет)
        if pid not in tanks:
            continue
        for g, ts in per.items():
            c = base_of[g]
            if c["kind"] == "self":
                spans[pid] += [(t, t + float(c["dur"]), g, pid) for t in ts]
    for t, src, tgt, g in ext_casts:
        if tgt in tanks:
            spans[tgt].append((t, t + float(base_of[g]["dur"]), g, src))

    retail = site == "www"
    owned = {pid: {g for g, c in base_of.items() if _fits(c, players[pid]) and (c.get("base") or g in presses.get(pid, {}))}
             for pid in players}

    def ready(pid: int, g: int, t: float) -> bool:
        cd, ch = eff(pid, g)
        return sum(1 for x in presses.get(pid, {}).get(g, []) if t - cd < x <= t) < ch

    order = {"healer": 0, "tank": 1, "dps": 2}
    givers = sorted(players, key=lambda p: order.get(players[p].get("role"), 3))

    log_names = [a.get("name") or "" for a in ((raw.get("report") or {}).get("masterData") or {}).get("abilities") or []]
    latin = sum(1 for n in log_names if n.isascii()) > len(log_names) / 2   # лог на английском

    def label(g: int) -> str:
        n = nm(g)
        if n and not n.startswith("Spell "):
            return n
        return (base_of[g].get("en") if latin else None) or base_of[g]["name"]

    events = []
    for b in busters:
        sch = b["school"]
        per_tank_k = Counter()
        for o in sorted(b["occ"], key=lambda o: o["t"]):
            t, pid = o["t"], o["pid"]
            on = [s for s in spans.get(pid, []) if s[0] - 0.3 <= t <= s[1] + 0.3]
            own = list(dict.fromkeys(s[2] for s in on if s[3] == pid))
            ext = list(dict.fromkeys((s[2], s[3]) for s in on if s[3] != pid))
            died = any(d.get("id") == pid and t <= d["t"] <= o["t_end"] + DEATH_S for d in deaths or [])
            alive = not _dead_at(dead, pid, t)
            free_own, free_ext = [], []
            if retail and alive:
                free_own = [g for g in owned[pid] if base_of[g]["kind"] == "self" and float(base_of[g]["cd"]) >= MAJOR_CD
                            and base_of[g].get("plan", True) and _school_ok(base_of[g], sch) and g not in own
                            and ready(pid, g, t)]
                free_ext = [(g, h) for h in givers if h != pid and not _dead_at(dead, h, t) for g in owned[h]
                            if base_of[g]["kind"] == "external" and base_of[g].get("plan", True)
                            and _school_ok(base_of[g], sch) and (g, h) not in ext and ready(h, g, t)]
            # кулдаун «держал» под следующий удар: нажал его до того, как он откатился бы снова — не в упрёк
            held = [g for g in free_own if any(t < x < t + eff(pid, g)[0] for x in presses.get(pid, {}).get(g, []))]
            n, plabel = _phase_label(phases, t)
            mit = (1 - o["damage"] / o["unmitigated"]) if o.get("unmitigated") else None
            events.append({
                "t": t, "time": _fmt_t(t), "phase": n, "phase_label": plabel, "id": b["id"], "ability": b["name"],
                "k": per_tank_k[pid], "tank": players[pid]["name"], "pid": pid, "cls": players[pid].get("cls"),
                "spec": players[pid].get("spec"), "damage": o["damage"], "size": o["size"], "hits": o["hits"],
                "mitigated": round(mit, 3) if mit is not None else None,
                "absorbed": round(o["absorbed"] / o["damage"], 3) if o["damage"] else None,
                "own": [{"id": g, "name": label(g)} for g in own],
                "ext": [{"id": g, "name": label(g), "from": players[h]["name"] if h in players else "—"} for g, h in ext],
                "covered": bool(own or ext), "died": died,
                "ready": [label(g) for g in free_own], "ready_ids": free_own,
                "held": [label(g) for g in held], "bare": bool(not (own or ext) and (died or set(free_own) - set(held))),
                "ready_ext": [f"{label(g)} ({players[h]['name']})" for g, h in free_ext][:3],
                "ready_ext_ids": [[g, h] for g, h in free_ext]})
            per_tank_k[pid] += 1
    events.sort(key=lambda e: (e["t"], e["tank"]))
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
        for g, ts in sorted(presses.get(pid, {}).items(), key=lambda x: min(x[1])):
            c = base_of[g]
            if c["kind"] != "self":
                continue
            cd, ch = eff(pid, g)
            on_hit = [t for t in ts if any(t - 1 <= h <= t + float(c["dur"]) + 0.5 for h in hit_t[pid])]
            cds.append({"tank": players[pid]["name"], "cls": players[pid].get("cls"), "spec": players[pid].get("spec"),
                        "id": g, "name": label(g), "cd": cd, "times": ts, "time": ", ".join(_fmt_t(x) for x in ts),
                        "used": len(ts), "max_uses": max(len(ts), ch + int(alive_s // cd)), "on_hit": len(on_hit)})
        if not any(c["tank"] == players[pid]["name"] for c in cds):   # танк без кулдаунов из таблицы
            cds.append({"tank": players[pid]["name"], "cls": players[pid].get("cls"), "spec": players[pid].get("spec"),
                        "id": None, "name": "", "cd": None, "times": [], "time": "", "used": 0, "max_uses": None,
                        "on_hit": 0})
    T = {"busters": busters, "events": events, "cds": cds, "tanks": [p["name"] for p in tanks.values()],
         "duration": round(dur, 1), "top": None, "site": site, "en": latin,
         "light": [{"id": e["id"], "k": e["k"], "covered": e["covered"], "died": e["died"],
                    "cds": [x["id"] for x in e["own"] + e["ext"]], "cls": e["cls"]} for e in events],
         "_eff": {f"{pid}:{g}": eff(pid, g) for pid in players for g in owned[pid]},
         "_owned": {str(pid): sorted(owned[pid]) for pid in players}}
    plan_tank(T, players, phases)
    refresh(T)
    return T


def plan_tank(T: dict, players: dict, phases: list[dict]) -> None:
    """План на следующий пулл: на каждый танкбастер — свой кулдаун танка или внешний сейв, с учётом отката.
    Крупные удары — первыми и долгими кулдаунами; удары по одному танку в окне уже назначенного кулдауна —
    тем же кулдауном. Сначала — что сработало в этом бою, затем — что жмут лучшие киллы, затем — любой свободный.
    Только для основной игры: на Classic откаты другие."""
    from .raid import phase_at
    from .raid_top import CLASS_COLOR, mrt_line
    T["plan"], T["mrt"] = [], ""
    if (T.get("site") or "www") != "www" or not T.get("events"):
        return
    table = _table()
    base_of = {}
    for c in table.values():
        if not c.get("group") or _grp(c) not in base_of:
            base_of[_grp(c)] = c
    effd = T.get("_eff") or {}
    owned = {int(k): set(v) for k, v in (T.get("_owned") or {}).items()}

    def eff(pid, g):
        v = effd.get(f"{pid}:{g}")
        return (float(v[0]), int(v[1])) if v else (float(base_of[g]["cd"]), int(base_of[g].get("charges") or 1))

    top_cds = ((T.get("top") or {}).get("cds") or {})
    order = {"healer": 0, "tank": 1, "dps": 2}
    givers = sorted(players, key=lambda p: order.get(players[p].get("role"), 3))
    rank = {g: i for i, g in enumerate(base_of)}   # при равенстве — порядок таблицы
    used: dict = defaultdict(list)        # (игрок, группа) → запланированные нажатия
    active: dict = defaultdict(list)      # танк → [(начало, конец, pick)]
    picks: dict = {}

    def free(pid, g, t):
        cd, ch = eff(pid, g)
        return sum(1 for x in used[(pid, g)] if abs(x - t) < cd) < ch

    for e in sorted(T["events"], key=lambda e: -(e.get("size") or e["damage"])):
        pid, t = e["pid"], e["t"]
        if pid not in players:
            continue
        same = next((a for a in active[pid] if a[0] <= t <= a[1]), None)
        if same:
            picks[id(e)] = (same[2], "тот же кулдаун")
            continue
        sch = next((b.get("school", 0) for b in T.get("busters") or [] if b["id"] == e["id"]), 0)
        pref = [x["id"] for x in e["own"]] + [int(i) for i in (top_cds.get(str(e["id"])) or [])]
        own = [g for g in owned.get(pid, ()) if base_of[g]["kind"] == "self" and float(base_of[g]["cd"]) >= MAJOR_CD
               and base_of[g].get("plan", True) and _school_ok(base_of[g], sch)]
        own.sort(key=lambda g: (pref.index(g) if g in pref else 99, -float(base_of[g]["cd"]), rank.get(g, 999)))
        pick = None
        for g in own:
            if free(pid, g, t):
                pick = ((pid, g), "как в бою" if g in [x["id"] for x in e["own"]] else "как у топа" if g in pref else "свободный")
                break
        if pick is None:
            for h in givers:
                if h == pid:
                    continue
                g = next((g for g in sorted(owned.get(h, ()), key=lambda g: g not in pref)
                          if base_of[g]["kind"] == "external" and base_of[g].get("plan", True)
                          and _school_ok(base_of[g], sch) and free(h, g, t)), None)
                if g:
                    pick = ((h, g), "внешний")
                    break
        if pick:
            (who, g), why = pick
            used[(who, g)].append(t)
            active[pid].append((t - PRESS_LEAD_S, t - PRESS_LEAD_S + float(base_of[g]["dur"]), (who, g)))
            picks[id(e)] = ((who, g), why)
    rows = []
    for e in T["events"]:
        pid, t = e["pid"], e["t"]
        ph = phase_at(phases, t)
        press = max(t - PRESS_LEAD_S, ph["t"] if ph else 0.0)
        row = {"t": t, "time": e["time"], "phase_label": e["phase_label"], "ability": e["ability"], "id": e["id"],
               "tank": e["tank"], "pick": None, "why": "", "mrt": ""}
        got = picks.get(id(e))
        if got:
            (who, g), why = got
            row["why"] = why
            row["pick"] = {"player": players[who]["name"], "cls": players[who].get("cls"), "id": g,
                           "name": (T.get("en") and base_of[g].get("en")) or base_of[g]["name"], "external": who != pid}
            if why != "тот же кулдаун":
                n = ph["n"] if ph and (ph.get("n") or 0) > 1 else None
                line = mrt_line(press, "«" + e["ability"] + "»", [row["pick"]], n,
                                round(press - ph["t"], 1) if n else None, mech_id=e["id"])
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
    retail = (T.get("site") or "www") == "www"
    if not busters:
        T["hints"] = ["Танкбастеров не найдено: нет крупных ударов босса по танкам (кроме ближнего боя)"] if T.get("tanks") else []
        T["key"] = None
        return
    for b in busters[:3]:
        s = f"«{b['name']}»: ударов по танкам — {b['n']}, прикрыто кулдауном — {b['covered']}"
        tb = (top.get("abilities") or {}).get(str(b["id"]))
        if tb:
            tn = (tb.get("names_en") if T.get("en") else None) or tb.get("names")   # на языке лога
            s += f"; у лучших киллов — {tb['share']:.0%}" + (f", чаще всего: {', '.join(tn[:3])}" if tn else "")
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
    bare = [e for e in events if e.get("bare") and not e["died"]]
    if bare:
        s = ("Удар без кулдауна, хотя кулдаун был откатан и не ушёл на следующий удар: " + "; ".join(
            f"{e['time']} «{e['ability']}», {e['tank']} — {_list([r for r in e['ready'] if r not in e['held']], 2)}"
            for e in bare[:3]) + ("…" if len(bare) > 3 else ""))
        out.append(s)
        if len(bare) >= 2:
            keys.setdefault("bare", s)
    if not retail:
        out.append("Таблица защитных кулдаунов — по основной игре: на этой версии игры откаты другие, поэтому "
                   "«был готов» и план на следующий пулл не считаются; видно только, прикрыт ли удар")
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
                      "names": [table[c]["name"] for c, _ in names[ab].most_common(4) if c in table],
                      "names_en": [table[c].get("en") or table[c]["name"] for c, _ in names[ab].most_common(4) if c in table]}
                 for ab, v in per.items() if v}
    return {"kills": len(ks), "abilities": abilities,
            "cds": {ab: [c for c, _ in names[ab].most_common(6)] for ab in names}}

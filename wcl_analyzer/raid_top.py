"""Рейд против топ-гильдий: пики урона и рейдовые кулдауны на лучших киллах этого босса,
и план кулдаунов на следующий пулл.

Пики сопоставляются по механике и её номеру в бою («Ледяная волна» №3), а не по секундам:
у разных рейдов разный темп боя. Кулдауны сравниваются по номеру способности — названия
в отчётах на разных языках отличаются, а номер один и тот же.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from statistics import median

from .compare import _fmt_t, _n

TOP_KILLS = 5
MIN_SHARE = 0.4          # пик учитывается, если он есть хотя бы у 40% лучших киллов
PRESS_LEAD_S = 2.0       # время нажатия по умолчанию — за 2 с до начала пика
PRESS_WINDOW_S = (1, 5)  # в плане кулдаун жмётся за 1–5 с до пика

# Перезарядка рейдовых кулдаунов, с (приблизительно; если в логе видно чаще — берём из лога)
RAID_CD_COOLDOWN = {
    62618: 180, 64843: 180, 47536: 90, 246287: 90, 15286: 120, 265202: 720, 200183: 120, 421453: 240,
    98008: 180, 108280: 180, 108281: 120, 114052: 180, 207399: 180,
    740: 180, 33891: 180, 197721: 90, 391528: 120,
    31821: 180, 216331: 120, 200652: 90, 498: 60,
    115310: 180, 388615: 180, 322118: 120, 325197: 120,
    363534: 240, 359816: 120, 374227: 120, 370960: 180, 370537: 90,
    97462: 180, 51052: 120, 196718: 300, 64382: 180,
}
DEFAULT_CD_S = 180


def light_raid(client, code: str, fid: int, report: dict | None = None, difficulty: int | None = None,
               encounter_id: int | None = None) -> dict:
    """Облегчённый разбор боя: только урон по рейду и рейдовые кулдауны (один пакетный запрос).
    Возвращает результат analyze_raid: пики урона, тяжёлые моменты, нажатия рейдовых кулдаунов."""
    from .api import WCLError
    from .raid import RAID_CD_IDS, analyze_raid
    ids = ", ".join(str(x) for x in sorted(RAID_CD_IDS))
    report = report or client.report(code)
    f = next(x for x in report["fights"] if int(x["id"]) == int(fid))
    if difficulty and int(f.get("difficulty") or 0) and int(f["difficulty"]) != int(difficulty):
        raise LookupError("другая сложность")
    if encounter_id and f.get("encounterID") and int(f["encounterID"]) != int(encounter_id):
        raise LookupError("другой босс")
    s, e = float(f["startTime"]), float(f["endTime"])
    got = None
    if hasattr(client, "events_multi") and not getattr(client, "_no_batch", False):
        try:  # урон по рейду и рейдовые кулдауны — одним запросом
            got = client.events_multi(code, fid, s, e, {
                "taken": {"data_type": "DamageTaken"},
                "casts": {"data_type": "Casts", "filter_expression": f"ability.id in ({ids})"}})
        except WCLError:
            got = None
    if got is None:
        try:
            casts = client.events(code, fid, s, e, "Casts", filter_expression=f"ability.id in ({ids})")
        except (WCLError, TypeError):
            casts = client.events(code, fid, s, e, "Casts")
        got = {"taken": client.events(code, fid, s, e, "DamageTaken"), "casts": casts}
    raw = {"report": report, "fight": f, "details": client.player_details(code, fid),
           "taken": got["taken"], "casts": got["casts"], "deaths": [], "pulls": []}
    return analyze_raid(raw)


def fetch_top_kills(client, encounter_id: int, difficulty: int, n: int = TOP_KILLS, log=print,
                    progress=lambda x: None) -> list[dict]:
    """Лучшие киллы босса по скорости: пики урона по рейду и рейдовые кулдауны каждого."""
    from .api import WCLError
    from .collect import parallel_workers

    if not difficulty:
        raise LookupError("не удалось определить сложность боя")
    ranks = client.fight_rankings(encounter_id, difficulty, "speed")
    cands = [r for r in ranks if (r.get("report") or {}).get("code")][: n + 3]

    def load(rk):
        rep = rk["report"]
        code, fid = rep["code"], int(rep.get("fightID") or rep.get("fightId") or 0)
        try:
            R = light_raid(client, code, fid, difficulty=difficulty, encounter_id=encounter_id)
            guild = (rk.get("guild") or {}).get("name") or rk.get("name") or code
            return {"guild": guild, "duration": R["info"]["duration_s"], "code": code, "fight": fid,
                    "spikes": R["extras"]["spikes"], "cds": R["extras"]["raid_cds"]}
        except (WCLError, StopIteration, KeyError, LookupError) as ex:
            log(f"  пропущен килл {code}: {ex}")
            return None

    out = []
    with ThreadPoolExecutor(max_workers=parallel_workers(client)) as pool:
        for i, k in enumerate(pool.map(load, cands)):
            progress(min(1.0, (i + 1) / max(1, min(n, len(cands)))))
            if k and len(out) < n:
                out.append(k)
                log(f"  Килл {len(out)} из {n}: {k['guild']}, {_fmt_t(k['duration'])}")
    return out


def raid_cd_peaks(client, code: str, fid: int, log=print, progress=lambda x: None,
                  top_difficulty: int | None = None) -> dict | None:
    """Для разбора игрока: какие рейдовые защитные кулдауны были нажаты в моменты наибольшего
    урона по рейду — в вашем бою и у лучших киллов этого босса."""
    report = client.report(code)
    R = light_raid(client, code, fid, report)
    X = R["extras"]
    if not X.get("damage_timeline"):
        return None
    vs = None
    f = next(x for x in report["fights"] if int(x["id"]) == int(fid))
    if hasattr(client, "fight_rankings") and f.get("encounterID"):
        try:
            diff = int(top_difficulty or f.get("difficulty") or 0)  # по умолчанию — сложность вашего боя
            kills = fetch_top_kills(client, int(f["encounterID"]), diff, log=log, progress=progress)
            vs = compare_with_top(R, kills)
            if vs:
                from .config import DIFFICULTY_NAMES
                vs["difficulty"] = DIFFICULTY_NAMES.get(diff, "")
        except Exception as e:  # noqa: BLE001 — сравнение с топом не обязательно
            log(f"Сравнение с лучшими киллами недоступно: {e}")
    return {"info": {"duration_s": R["info"]["duration_s"]},
            "extras": {"damage_timeline": X.get("damage_timeline"), "heaviest": X.get("heaviest") or [],
                       "raid_cds": X.get("raid_cds") or [], "vs_top": vs},
            "brief": brief_lines(vs)}


def _aggregate(kills: list[dict]) -> dict:
    ref: dict = defaultdict(lambda: {"times": [], "peaks": [], "covered": 0, "kills": 0, "cds": Counter(),
                                     "name": None, "damage": []})
    for k in kills:
        for sp in k["spikes"]:
            r = ref[(sp.get("ability_id"), sp.get("k", 1))]
            r["times"].append(sp["t"])
            r["peaks"].append(sp.get("peak_t", sp["t"] + 2))
            r["kills"] += 1
            r["damage"].append(sp["damage"])
            r["name"] = r["name"] or sp["ability"]
            if sp.get("covered_ids"):
                r["covered"] += 1
            r["cds"].update(sp.get("covered_ids") or [])
    need = max(2, MIN_SHARE * len(kills)) if len(kills) >= 3 else 1
    return {key: r for key, r in ref.items() if r["kills"] >= need}


def compare_with_top(R: dict, kills: list[dict]) -> dict | None:
    """Сравнение пиков и кулдаунов вашего боя с лучшими киллами + план на следующий пулл."""
    if not kills:
        return None
    X = R["extras"]
    names: dict = {}
    for k in kills:
        for c in k["cds"]:
            names.setdefault(c["id"], c["name"])
    for c in X.get("raid_cds", []):
        names[c["id"]] = c["name"]  # названия из вашего отчёта (ваш язык) важнее
    ref = _aggregate(kills)

    rows, matched = [], set()
    for sp in X.get("spikes", []):
        key = (sp.get("ability_id"), sp.get("k", 1))
        r = ref.get(key)
        matched.add(key)
        rows.append({"t": sp["t"], "time": sp["time"], "mechanic": f"«{sp['ability']}» №{sp.get('k', 1)}",
                     "my": sp.get("covered_by") or [],
                     "top_share": (r["covered"] / r["kills"]) if r else None,
                     "top_cds": [names.get(i, f"#{i}") for i, _ in r["cds"].most_common(2)] if r else [],
                     "top_time": _fmt_t(median(r["times"])) if r else None})
    late = []
    for key, r in ref.items():
        if key in matched:
            continue
        t = median(r["times"])
        if t <= (R["info"].get("duration_s") or 0):
            continue  # такой пик у топа есть, но в вашем бою он прошёл тихо — не повод для плана
        late.append({"t": t, "peak_t": median(r["peaks"]), "time": _fmt_t(t), "mechanic": f"«{r['name']}» №{key[1]}", "my": None,
                     "top_share": r["covered"] / r["kills"],
                     "top_cds": [names.get(i, f"#{i}") for i, _ in r["cds"].most_common(2)],
                     "top_time": _fmt_t(t), "_key": key})
    rows += late

    my_cover = (sum(1 for s in X.get("spikes", []) if s.get("covered_by")) / len(X["spikes"])) if X.get("spikes") else None
    tot = sum(r["kills"] for r in ref.values())
    top_cover = (sum(r["covered"] for r in ref.values()) / tot) if tot else None
    plan = make_plan(X, ref, late, names)
    marks = [{"t": median(r["times"]), "name": ", ".join(names.get(i, f"#{i}") for i, _ in r["cds"].most_common(2))}
             for r in ref.values() if r["kills"] and r["covered"] / r["kills"] >= 0.5]
    return {"kills": [{"guild": k["guild"], "duration": _fmt_t(k["duration"]),
                       "url": f"https://www.warcraftlogs.com/reports/{k['code']}#fight={k['fight']}"} for k in kills],
            "n": len(kills), "rows": [{kk: v for kk, v in r.items() if not kk.startswith("_")}
                                      for r in sorted(rows, key=lambda r: r["t"])],
            "my_cover": my_cover, "top_cover": top_cover, "plan": plan,
            "marks": sorted(marks, key=lambda m: m["t"])}


def make_plan(X: dict, ref: dict, late: list[dict], names: dict) -> list[dict]:
    """Кто какой кулдаун жмёт на каждый пик следующего пулла.

    Доступные кулдауны — те, что ваш рейд нажимал в этом бою (о кулдаунах, которые никто не
    нажал, лог ничего не знает). Сначала закрываются самые тяжёлые пики; если топ на этот пик
    жмёт определённый кулдаун и он у вас есть — берётся он. Перезарядка не нарушается."""
    cds: dict = {}
    uses = defaultdict(list)
    for c in X.get("raid_cds", []):
        uses[(c["pid"], c["id"])].append(c["t"])
        cds[(c["pid"], c["id"])] = {"player": c["player"], "name": c["name"], "id": c["id"]}
    for key, ts in uses.items():
        ts.sort()
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        known = RAID_CD_COOLDOWN.get(key[1])
        seen = min(gaps) if gaps else None
        cds[key]["cd"] = min(v for v in (known, seen) if v) if (known or seen) else DEFAULT_CD_S
    events = [{"t": s.get("peak_t", s["t"]), "mechanic": f"«{s['ability']}» №{s.get('k', 1)}", "key": (s.get("ability_id"), s.get("k", 1)),
               "prio": s["damage"]} for s in X.get("spikes", [])]
    top_dmg = max((e["prio"] for e in events), default=1.0)
    for r in late:
        events.append({"t": r["peak_t"], "mechanic": r["mechanic"], "key": r["_key"], "prio": top_dmg * 0.5 * (r["top_share"] or 0.5)})
    assigned: dict = defaultdict(list)
    plan = []
    # По ходу боя: каждый пик получает кулдаун, нажатый как можно раньше в окне 1–5 с до пика,
    # чтобы следующий раз он откатился как можно раньше. Сначала — кулдаун, который жмёт топ.
    for ev in sorted(events, key=lambda e: e["t"]):
        r = ref.get(ev["key"])
        pref = [i for i, _ in r["cds"].most_common()] if r else []
        best = None
        for k in sorted(cds, key=lambda k: (pref.index(k[1]) if k[1] in pref else 99, len(assigned[k]))):
            for lead in range(PRESS_WINDOW_S[1], PRESS_WINDOW_S[0] - 1, -1):
                at = max(0.0, ev["t"] - lead)
                if all(abs(at - t) >= cds[k]["cd"] for t in assigned[k]):
                    best = (k, at)
                    break
            if best:
                break
        row = {"time": _fmt_t(best[1] if best else max(0.0, ev["t"] - PRESS_LEAD_S)),
               "t": best[1] if best else max(0.0, ev["t"] - PRESS_LEAD_S), "mechanic": ev["mechanic"],
               "top": ", ".join(names.get(i, f"#{i}") for i in pref[:2])}
        if best:
            k, at = best
            assigned[k].append(at)
            row.update({"cd": cds[k]["name"], "player": cds[k]["player"], "like_top": k[1] in pref[:2]})
        else:
            row.update({"cd": None, "player": None, "like_top": False})
        plan.append(row)
    return sorted(plan, key=lambda r: r["t"])


def brief_lines(vs: dict | None) -> list[str]:
    if not vs:
        return []
    out = []
    if vs["top_cover"] is not None and vs["my_cover"] is not None:
        out.append(f"Лучшие киллы ({_n(vs['n'], 'килл', 'килла', 'киллов')}) закрывают рейдовыми кулдаунами "
                   f"{vs['top_cover']:.0%} пиков урона, ваш рейд — {vs['my_cover']:.0%}")
    gaps = [r for r in vs["rows"] if r["my"] is not None and not r["my"] and (r["top_share"] or 0) >= 0.5]
    if gaps:
        g = gaps[0]
        out.append(f"{g['time']} {g['mechanic']}: у вас без кулдауна, у топа — "
                   f"{', '.join(g['top_cds']) or 'кулдаун'} в {g['top_share']:.0%} киллов")
    return out

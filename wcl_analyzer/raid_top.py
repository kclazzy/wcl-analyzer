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
PHASE_SHIFT_S = 10.0     # запас к перезарядке, если два нажатия в разных фазах: фаза может начаться раньше

DEFAULT_CD_S = 180


def light_raid(client, code: str, fid: int, report: dict | None = None, difficulty: int | None = None,
               encounter_id: int | None = None) -> dict:
    """Облегчённый разбор боя (лучшие киллы): только урон по рейду и рейдовые кулдауны, один пакетный запрос."""
    from .api import WCLError
    from . import game_data
    from .raid import analyze_raid
    ids = ", ".join(str(x) for x in sorted(game_data.cd_ids()))
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


def _roster_lines(roster: list[dict], plan: list[dict]) -> list[str]:
    out = []
    unused = [c for c in roster if not c["used"]]
    if unused:
        out.append(f"Рейдовых кулдаунов в составе: {len(roster)}, ни разу не нажато: {len(unused)} — "
                   + ", ".join(f"{c['name']} ({c['player']})" for c in unused[:4]) + ("…" if len(unused) > 4 else ""))
    empty = [r for r in plan if not r.get("picks")]
    if empty:
        out.append("Пики без свободного кулдауна в плане: " + ", ".join(f"{r['time']} {r['mechanic']}" for r in empty[:3]))
    return out


def _phase_keys(spikes: list[dict]) -> dict:
    """id(пик) → (способность, фаза, номер повторения внутри фазы). Без фаз — пусто."""
    out, cnt = {}, Counter()
    for sp in sorted(spikes, key=lambda x: x["t"]):
        if sp.get("phase"):
            cnt[(sp.get("ability_id"), sp["phase"])] += 1
            out[id(sp)] = (sp.get("ability_id"), sp["phase"], cnt[(sp.get("ability_id"), sp["phase"])])
    return out


def _aggregate(kills: list[dict], by_phase: bool = False) -> dict:
    """Пики лучших киллов, сведённые по ключу: (способность, №) или, с by_phase, (способность, фаза, № в фазе)."""
    ref: dict = defaultdict(lambda: {"times": [], "peaks": [], "covered": 0, "kills": 0, "cds": Counter(),
                                     "name": None, "damage": [], "n_cds": [], "ph": []})
    for k in kills:
        pk = _phase_keys(k["spikes"]) if by_phase else {}
        for sp in k["spikes"]:
            if by_phase and id(sp) not in pk:
                continue
            r = ref[pk[id(sp)] if by_phase else (sp.get("ability_id"), sp.get("k", 1))]
            r["times"].append(sp["t"])
            r["peaks"].append(sp.get("peak_t", sp["t"] + 2))
            if sp.get("phase"):
                r["ph"].append((sp["phase"], sp.get("phase_t") or 0.0, sp.get("peak_t", sp["t"] + 2) - sp["t"]))
            r["kills"] += 1
            r["damage"].append(sp["damage"])
            r["name"] = r["name"] or sp["ability"]
            if sp.get("covered_ids"):
                r["covered"] += 1
            r["cds"].update(sp.get("covered_ids") or [])
            r["n_cds"].append(len(sp.get("covered_ids") or []))  # сколько кулдаунов топ жмёт на этот пик
    need = max(2, MIN_SHARE * len(kills)) if len(kills) >= 3 else 1
    return {key: r for key, r in ref.items() if r["kills"] >= need}


def _merge(rs: list[dict]) -> dict | None:
    if not rs:
        return None
    m = {"times": [], "peaks": [], "covered": 0, "kills": 0, "cds": Counter(), "name": rs[0]["name"],
         "damage": [], "n_cds": [], "ph": []}
    for r in rs:
        for f in ("times", "peaks", "damage", "n_cds", "ph"):
            m[f] += r.get(f, [])
        m["covered"] += r.get("covered", 0)
        m["kills"] += r.get("kills", 0)
        m["cds"].update(r.get("cds", Counter()))
    return m


def resolver(ref: dict, ref_ph: dict | None = None):
    """Аналог вашего пика у лучших киллов → (данные топа, как сопоставлено).

    1) «phase» — та же способность, та же фаза и тот же номер повторения внутри фазы. Так пики не
       «съезжают», если ваша фаза длиннее, чем у топа, и способность в ней повторяется больше раз;
    2) «k» — та же способность и тот же номер повторения за бой;
    3) «template» — такого повторения у топа нет (ваш бой или фаза длиннее): берём, что топ жмёт на эту
       же способность в других повторениях (сначала — в этой же фазе), и сколько кулдаунов ставит;
    4) None — этой способности у топа нет вовсе."""
    ref_ph = ref_ph or {}

    def find(ab, k, phase=None, kp=None):
        if phase and kp and (ab, phase, kp) in ref_ph:
            return ref_ph[(ab, phase, kp)], "phase"
        if (ab, k) in ref and not (phase and any(x[0] == ab and x[1] == phase for x in ref_ph)):
            return ref[(ab, k)], "k"
        same = [r for key, r in ref_ph.items() if key[0] == ab and key[1] == phase] if phase else []
        tpl = _merge(same) or _merge([r for key, r in ref.items() if key[0] == ab])
        return (tpl, "template") if tpl else (None, None)
    return find


def _split_scope(row: dict, r: dict | None, names: dict) -> None:
    from .game_data import scope
    row["top_raid"], row["top_self"] = [], []
    if not r:
        return
    for sid, _ in r["cds"].most_common():
        lst = row["top_self"] if scope(sid) == "self" else row["top_raid"]
        if len(lst) < 2:
            lst.append(names.get(sid, f"#{sid}"))


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
    ref_ph = _aggregate(kills, by_phase=True)
    find = resolver(ref, ref_ph)
    my_pk = _phase_keys(X.get("spikes", []))

    rows, matched = [], set()
    for sp in X.get("spikes", []):
        key = (sp.get("ability_id"), sp.get("k", 1))
        pk = my_pk.get(id(sp))
        r, how = find(key[0], key[1], *(pk[1:] if pk else (None, None)))
        if how == "template":
            r = None  # в таблице сравнения — только настоящий аналог у топа
        matched.add(key)
        own = set(sp.get("covered_self") or [])
        rows.append({"t": sp["t"], "time": sp["time"], "mechanic": f"«{sp['ability']}» №{sp.get('k', 1)}",
                     "my": sp.get("covered_by") or [], "_k": key,
                     "my_self": [x for x in sp.get("covered_by") or [] if x in own],
                     "my_raid": [x for x in sp.get("covered_by") or [] if x not in own],
                     "top_share": (r["covered"] / r["kills"]) if r else None,
                     "top_cds": [names.get(i, f"#{i}") for i, _ in r["cds"].most_common(2)] if r else [],
                     "top_time": _fmt_t(median(r["times"])) if r else None, "_r": r})
    late = []
    for key, r in ref.items():
        if key in matched:
            continue
        t = median(r["times"])
        if t <= (R["info"].get("duration_s") or 0):
            continue  # такой пик у топа есть, но в вашем бою он прошёл тихо — не повод для плана
        peak_t = median(r["peaks"])
        ph_n, ph_t = _top_phase(r)
        mine = next((p for p in R["info"].get("phases") or [] if p["n"] == ph_n), None)
        if mine and ph_t is not None:  # ваш бой дошёл до этой фазы: время — от её начала у вас
            t = mine["t"] + ph_t
            peak_t = t + median(x[2] for x in r["ph"] if x[0] == ph_n)
            if t <= (R["info"].get("duration_s") or 0):
                continue  # по вашим фазам это время ваш бой прошёл, и пика не было — топ здесь не указ
        late.append({"t": t, "peak_t": peak_t, "phase": ph_n, "phase_t": ph_t, "time": _fmt_t(t), "mechanic": f"«{r['name']}» №{key[1]}", "my": None,
                     "top_share": r["covered"] / r["kills"],
                     "top_cds": [names.get(i, f"#{i}") for i, _ in r["cds"].most_common(2)],
                     "top_time": _fmt_t(t), "_key": key, "_k": key, "my_self": None, "my_raid": None, "after_end": True})
    rows += late
    for row in rows:  # на рейд и на себя (усиление лекаря) — раздельно
        r = row.pop("_r") if "_r" in row else (ref.get(row.pop("_k", None)) if "_k" in row else None)
        row.pop("_k", None)
        _split_scope(row, r, names)

    my_cover = (sum(1 for s in X.get("spikes", []) if s.get("covered_by")) / len(X["spikes"])) if X.get("spikes") else None
    tot = sum(r["kills"] for r in ref.values())
    top_cover = (sum(r["covered"] for r in ref.values()) / tot) if tot else None
    plan = make_plan(X, ref, late, names, R["info"].get("phases"), ref_ph)
    marks = [{"t": median(r["times"]), "name": ", ".join(names.get(i, f"#{i}") for i, _ in r["cds"].most_common(2))}
             for r in ref.values() if r["kills"] and r["covered"] / r["kills"] >= 0.5]
    return {"kills": [{"guild": k["guild"], "duration": _fmt_t(k["duration"]),
                       "url": f"https://www.warcraftlogs.com/reports/{k['code']}#fight={k['fight']}"} for k in kills],
            "n": len(kills), "rows": [{kk: v for kk, v in r.items() if not kk.startswith("_")}
                                      for r in sorted(rows, key=lambda r: r["t"])],
            "my_cover": my_cover, "top_cover": top_cover, "plan": plan,
            "marks": sorted(marks, key=lambda m: m["t"])}


from . import game_data  # noqa: E402

HEAVY_PEAK = 1.25    # пик тяжелее медианы пиков в 1,25 раза — на него два кулдауна
DANGER_PEAK = 1.6    # в 1,6 раза — три
MAX_PER_PEAK = 3
MAX_SPARE = 3        # запасных вариантов на пик
MAX_HEAL_PER_PEAK = 2  # кулдаунов лекарей на один пик (разные лекари)


def _top_phase(r: dict) -> tuple[int | None, float | None]:
    """В какой фазе этот пик у топа и через сколько секунд от её начала (медиана по киллам)."""
    if not r.get("ph"):
        return None, None
    n = Counter(x[0] for x in r["ph"]).most_common(1)[0][0]
    return n, median(x[1] for x in r["ph"] if x[0] == n)


def make_plan(X: dict, ref: dict, late: list[dict], names: dict, phases: list[dict] | None = None,
              ref_ph: dict | None = None) -> list[dict]:
    """Кто какой кулдаун жмёт на каждый пик следующего пулла.

    Доступные кулдауны — из состава рейда (X["roster_cds"]: класс, спек, взятые таланты, перезарядка);
    без данных о составе — те, что рейд нажимал в этом бою. Пики идут по ходу боя; на каждый — кулдаун,
    нажатый как можно раньше в окне 1–5 с до пика, чтобы следующий раз он откатился раньше. Сначала —
    кулдаун, который на этот пик жмёт топ; на лёгкие пики — короткие кулдауны, длинные бережём для
    тяжёлых. На самые тяжёлые пики — два кулдауна разных игроков. Перезарядка не нарушается.

    Фазы: если два нажатия одного кулдауна в разных фазах, к перезарядке добавляется запас PHASE_SHIFT_S —
    в следующем пулле фаза может начаться раньше (больше урона по боссу), и интервал между нажатиями сократится.
    Время в плане и в заметке MRT для второй фазы и дальше — от начала фазы.

    Ваш бой длиннее, чем у топа: пики сопоставляются по фазе и номеру повторения в фазе (resolver), а пикам,
    которых у топа нет, план берёт образец с этой же способности у топа — какие кулдауны и сколько."""
    phases = phases or []

    def ph_n(t: float) -> int | None:
        cur = None
        for p in phases:
            if p["t"] <= t + 1e-6:
                cur = p["n"]
        return cur

    def free(k, at: float) -> bool:
        return all(abs(at - t) >= cds[k]["cd"] + (PHASE_SHIFT_S if ph_n(at) != ph_n(t) else 0.0)
                   for t in assigned[k])
    cds: dict = {}
    pool = X.get("roster_cds")
    if pool:
        for c in pool:
            cds[(c["pid"], c["id"])] = {"player": c["player"], "name": c["name"], "id": c["id"], "cd": float(c["cd"]),
                                        "cls": c.get("cls") or ""}
    else:
        uses = defaultdict(list)
        for c in X.get("raid_cds", []):
            uses[(c["pid"], c["id"])].append(c["t"])
            cds[(c["pid"], c["id"])] = {"player": c["player"], "name": c["name"], "id": c["id"], "cls": c.get("cls") or ""}
        for key, ts in uses.items():
            ts.sort()
            gaps = [b - a for a, b in zip(ts, ts[1:])]
            known = game_data.cooldown(key[1])
            seen = min(gaps) if gaps else None
            cds[key]["cd"] = min(v for v in (known, seen) if v) if (known or seen) else DEFAULT_CD_S
    find = resolver(ref, ref_ph)
    my_pk = _phase_keys(X.get("spikes", []))
    events = [{"t": s.get("peak_t", s["t"]), "mechanic": f"«{s['ability']}» №{s.get('k', 1)}", "key": (s.get("ability_id"), s.get("k", 1)),
               "prio": s["damage"], "deaths": s.get("deaths", 0), "phase": s.get("phase"),
               "pk": my_pk.get(id(s))} for s in X.get("spikes", [])]
    top_dmg = max((e["prio"] for e in events), default=1.0)
    for r in late:
        events.append({"t": r["peak_t"], "mechanic": r["mechanic"], "key": r["_key"], "prio": top_dmg * 0.5 * (r["top_share"] or 0.5),
                       "phase": r.get("phase"), "phase_start": (r["t"] - r["phase_t"]) if r.get("phase_t") is not None else None,
                       "after_end": True})
    med = median([e["prio"] for e in events]) if events else 0
    assigned: dict = defaultdict(list)
    states = []
    for ev in events:
        if "pk" in ev:  # пик вашего боя
            r, how = find(ev["key"][0], ev["key"][1], *(ev["pk"][1:] if ev.get("pk") else (None, None)))
        else:  # пик из «late» — он и так взят у топа
            r, how = ref.get(ev["key"]), "k"
        ev["how"] = how
        pref = [i for i, _ in r["cds"].most_common()] if r else []
        # Сколько кулдаунов: по тяжести пика и по тому, сколько на него жмёт топ (медиана по киллам).
        # Пики, которых у топа нет (ваш бой длиннее — топ убил босса раньше), — только по тяжести
        sev = (1 + (bool(med) and ev["prio"] >= HEAVY_PEAK * med) + (bool(med) and ev["prio"] >= DANGER_PEAK * med)
               + (ev.get("deaths", 0) > 0))  # в этот пик в вашем бою кто-то умер
        top_need = int(round(median(r["n_cds"]))) if r and r.get("n_cds") else 0
        need = max(1, min(MAX_PER_PEAK, max(sev, top_need), len({c["player"] for c in cds.values()})))
        states.append({"ev": ev, "r": r, "pref": pref, "top_need": top_need, "need": need, "picks": [], "players": set(),
                       "how": how})

    # Рейдовые сейвы и кулдауны лекарей на себя (Апофеоз, Древо Жизни…) планируются раздельно:
    # сейвы — по пикам, как у топа; кулдауны лекарей — вторым проходом на самые тяжёлые пики, своей заметкой
    raid_keys = [k for k in cds if game_data.scope(k[1]) != "self"]
    heal_keys = [k for k in cds if game_data.scope(k[1]) == "self"]

    def add_one(st) -> bool:
        ev, pref, picks, players = st["ev"], st["pref"], st["picks"], st["players"]
        # Сначала кулдаун, который здесь жмёт топ; затем — реже назначенный и более сильный
        order = sorted(raid_keys, key=lambda k: (pref.index(k[1]) if k[1] in pref else 99, len(assigned[k]),
                                           -game_data.power(k[1]), cds[k]["cd"]))
        for k in order:
            # на один пик — разные игроки и разные способности: два одинаковых кулдауна
            # одного класса (два «Ободряющих клича», два гимна) не складываются в пользу рейда
            if any(k == p[0] or k[1] == p[0][1] for p in picks) or cds[k]["player"] in players:
                continue
            for lead in range(PRESS_WINDOW_S[1], PRESS_WINDOW_S[0] - 1, -1):
                at = max(0.0, ev["t"] - lead)
                if free(k, at):
                    assigned[k].append(at)
                    players.add(cds[k]["player"])
                    picks.append((k, at))
                    return True
        return False

    # Сначала — по одному сейву на каждый пик, от самых тяжёлых к лёгким: иначе кулдауны уходят на ранние
    # лёгкие пики, а сильный урон в конце длинного боя (у топа его нет — они убивают раньше) остаётся без сейва.
    # Потом — добираем второй и третий сейв на тяжёлые пики.
    # Пики после конца вашего боя (время — по лучшим киллам) — в последнюю очередь: кулдауны сначала на ваши
    # настоящие пики, по их фактическому времени
    by_weight = sorted(states, key=lambda st: (bool(st["ev"].get("after_end")), -st["ev"]["prio"], -st["need"], st["ev"]["t"]))
    for st in by_weight:
        add_one(st)
    for st in by_weight:
        while len(st["picks"]) < st["need"] and add_one(st):
            pass
    # Кулдауны лекарей: самые тяжёлые пики первыми, на пик — до MAX_HEAL_PER_PEAK разных лекарей,
    # перезарядка и запас на смещение фазы — как у сейвов
    for st in by_weight:
        st["heal"] = []
        for k in sorted(heal_keys, key=lambda k: (len(assigned[k]), -game_data.power(k[1]), cds[k]["cd"])):
            if len(st["heal"]) >= MAX_HEAL_PER_PEAK:
                break
            if any(cds[k]["player"] == cds[h]["player"] or k[1] == h[1] for h, _ in st["heal"]):
                continue
            for lead in range(PRESS_WINDOW_S[1], PRESS_WINDOW_S[0] - 1, -1):
                at = max(0.0, st["ev"]["t"] - lead)
                if free(k, at):
                    assigned[k].append(at)
                    st["heal"].append((k, at))
                    break
    plan = []
    for st in sorted(states, key=lambda st: st["ev"]["t"]):
        ev, r, pref, top_need, need = st["ev"], st["r"], st["pref"], st["top_need"], st["need"]
        heavy = need >= 2
        picks = sorted(st["picks"], key=lambda p: p[1])
        t0 = picks[0][1] if picks else max(0.0, ev["t"] - PRESS_LEAD_S)
        # фаза: из вашего боя; пики, до которых вы не дошли, — из лучших киллов
        n = ev.get("phase") or ph_n(t0)
        pinfo = next((p for p in phases if p["n"] == n), None)
        start = pinfo["t"] if pinfo else ev.get("phase_start")
        if n and start is None and n == 1:
            start = 0.0
        rel = max(0.0, t0 - start) if start is not None else None
        row = {"time": _fmt_t(t0), "t": t0, "mechanic": ev["mechanic"], "heavy": bool(heavy), "need": need,
               "phase": n, "phase_name": (pinfo or {}).get("name") or (f"Фаза {n}" if n else ""),
               "intermission": bool((pinfo or {}).get("intermission")),
               "phase_time": _fmt_t(rel) if rel is not None else None, "phase_t": rel,
               "deaths": ev.get("deaths", 0),
               # у лучших киллов такого пика нет (бой или фаза у них короче): «template» — сейвы по образцу
               # этой же способности у топа, иначе — только по тяжести пика
               "beyond_top": bool(ref) and st["how"] in (None, "template"),
               "by_template": st["how"] == "template",
               # пика нет в вашем бою (он кончился раньше): время взято у лучших киллов — в заметки MRT не входит
               "after_end": bool(ev.get("after_end")),
               "top_n": top_need,
               "top": ", ".join(names.get(i, f"#{i}") for i in pref[:2]),
               "picks": [{"cd": cds[k]["name"], "player": cds[k]["player"], "like_top": k[1] in pref[:2],
                          "cooldown": _fmt_t(cds[k]["cd"]), "ready": _fmt_t(at + cds[k]["cd"]), "at": _fmt_t(at),
                          "id": cds[k]["id"], "cls": cds[k].get("cls") or game_data.class_of(cds[k]["id"])}
                         for k, at in picks]}
        row["mrt"] = mrt_line(t0, ev["mechanic"], row["picks"], n if (n or 0) > 1 else None, rel,
                              mech_id=(ev.get("key") or (None,))[0])
        heal = sorted(st.get("heal") or [], key=lambda p: p[1])
        row["heal_picks"] = [{"cd": cds[k]["name"], "player": cds[k]["player"], "cooldown": _fmt_t(cds[k]["cd"]),
                              "ready": _fmt_t(at + cds[k]["cd"]), "at": _fmt_t(at), "t": at, "id": cds[k]["id"],
                              "cls": cds[k].get("cls") or game_data.class_of(cds[k]["id"])} for k, at in heal]
        if heal:  # своё время — первое нажатие лекаря, со 2-й фазы — от начала фазы
            th = heal[0][1]
            nh = ph_n(th)
            ph_info = next((p for p in phases if p["n"] == nh), None)
            sh = ph_info["t"] if ph_info else None
            row["heal_time"] = _fmt_t(th)
            row["mrt_heal"] = mrt_line(th, ev["mechanic"], row["heal_picks"], nh if (nh or 0) > 1 else None,
                                       max(0.0, th - sh) if sh is not None else None, mech_id=(ev.get("key") or (None,))[0])
            row["heal_t"] = th
        if picks:
            f = row["picks"][0]
            row.update({"cd": f["cd"], "player": f["player"], "like_top": any(x["like_top"] for x in row["picks"])})
        else:
            row.update({"cd": None, "player": None, "like_top": False})
        row["_t"] = ev["t"]
        row["_keys"] = [k for k, _ in picks] + [k for k, _ in heal]
        plan.append(row)
    # Запасные варианты: кулдауны, которые к этому пику откатаны и не мешают остальному плану
    for row in plan:
        at = max(0.0, row["_t"] - PRESS_LEAD_S)
        spare = [k for k in cds if k not in row["_keys"] and free(k, at)]
        busy = {cds[k]["player"] for k in row["_keys"]}  # сначала — другие игроки, не те, кто уже жмёт
        spare.sort(key=lambda k: (cds[k]["player"] in busy, -game_data.power(k[1]), cds[k]["cd"]))
        seen, out = {k[1] for k in row["_keys"]}, []  # запасные — без повторов уже назначенной способности
        for k in spare:
            if k[1] in seen:
                continue
            seen.add(k[1])
            out.append({"cd": cds[k]["name"], "player": cds[k]["player"], "cooldown": _fmt_t(cds[k]["cd"])})
            if len(out) >= MAX_SPARE:
                break
        row["spare"] = out
        del row["_t"], row["_keys"]
    return sorted(plan, key=lambda r: r["t"])


# Цвета классов WoW — так MRT раскрашивает имена в заметке (|cffRRGGBBИмя|r)
CLASS_COLOR = {"Warrior": "C69B6D", "Paladin": "F48CBA", "Hunter": "AAD372", "Rogue": "FFF468", "Priest": "FFFFFF",
               "DeathKnight": "C41E3A", "Shaman": "0070DD", "Mage": "3FC7EB", "Warlock": "8788EE", "Monk": "00FF98",
               "Druid": "FF7C0A", "DemonHunter": "A330C9", "Evoker": "33937F"}


def _mrt_time(t: float) -> str:
    t = max(0, int(round(t)))
    return f"{t // 60}:{t % 60:02d}"


def mrt_line(t: float, mechanic: str, picks: list[dict], phase: int | None = None, phase_t: float | None = None,
             mech_id: int | None = None) -> str:
    """Строка заметки Method Raid Tools: {time:м:сс} — таймер от пулла, {time:м:сс,p2} — от начала 2-й фазы,
    {spell:id} — иконка способности: и механика босса, и кулдауны показаны иконками — так строка короче.
    Название механики — только если её id неизвестен. Пустая строка, если на пик нет кулдауна."""
    if not picks:
        return ""
    by: dict = {}  # один игрок с двумя кулдаунами — имя один раз, за ним обе иконки
    for p in picks:
        color = CLASS_COLOR.get(p.get("cls") or "")
        name = f"|cff{color}{p['player']}|r" if color else p["player"]
        mark = f"{{spell:{p['id']}}}" if p.get("id") else p["cd"]
        if mark not in by.setdefault(name, []):
            by[name].append(mark)
    who = [f"{name} " + " ".join(marks) for name, marks in by.items()]
    tm = f"{_mrt_time(phase_t)},p{phase}" if phase and phase_t is not None else _mrt_time(t)
    mech = f"{{spell:{mech_id}}}" if mech_id and int(mech_id) > 0 else mechanic.replace("«", "").replace("»", "")
    return f"{{time:{tm}}}{mech} - " + "  ".join(who)


def mrt_note(plan: list[dict], title: str = "") -> str:
    """Весь план сейвов одной заметкой для MRT (вставить в Заметки → Общая заметка). Только пики вашего боя
    по их фактическому времени: пики, до которых бой не дошёл (время у них — от лучших киллов), не входят."""
    lines = [r.get("mrt") for r in plan if r.get("mrt") and not r.get("after_end")]
    if not lines:
        return ""
    head = [f"Сейвы: {title}" if title else "Сейвы"]
    return "\n".join(head + lines)


def mrt_heal_note(plan: list[dict], title: str = "") -> str:
    """Кулдауны лекарей из плана — отдельной заметкой MRT, по времени своих нажатий."""
    rows = sorted((r for r in plan if r.get("mrt_heal") and not r.get("after_end")), key=lambda r: r.get("heal_t", r["t"]))
    if not rows:
        return ""
    return "\n".join([f"Кулдауны лекарей: {title}" if title else "Кулдауны лекарей"] + [r["mrt_heal"] for r in rows])


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

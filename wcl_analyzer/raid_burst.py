"""Нанесение урона: когда рейд жмёт героизм, бурсты (крупные боевые кулдауны DPS) и внешние усиления
(Придание сил), когда на боссе окна механик, под которые жмут бурсты, и совпадает ли одно с другим.

Зеркало плана сейвов: там — входящий урон и сейвы, здесь — исходящий урон и бурсты. Кулдауны — из таблицы
игровых данных (game_data.json → dps_cds, номера проверены на Wowhead). Окна на боссе — баффы и дебаффы
на боссе от самой механики боя (не от игроков). «Окно для бурста» наверняка — если механика есть в гайде
Mythic Trap (тип «усиление урона» или «щит») или в таблице game_data.json → amp_windows; «возможно» — если
по названию или описанию босс получает больше урона.
"""
from __future__ import annotations

import re
from collections import defaultdict
from statistics import median

from . import game_data
from .compare import _fmt_t
from .metrics import LUST_IDS, LUST_RE

LUST_LEAD_S = 5        # бурст «под героизм»: от 5 с до героизма …
LUST_TAIL_S = 25       # … до 25 с после (героизм длится 40 с)
LUST_SAME_S = 60       # героизмы ближе минуты друг к другу (несколько шаманов) — один героизм
WIN_LEAD_S = 10        # бурст «в окно»: нажат не раньше 10 с до начала окна и до его конца
WIN_LATE_S = 20        # нажал в первые 20 с после окна — опоздал, но не «держал»
MIN_WIN_S = 4          # окна короче 4 с — не окна
MAIN_CD_S = 90         # «главный» бурст игрока — самый долгий, от 1,5 мин
CAST_WIN_S = 15        # механика из гайда найдена только кастом босса — окно 15 с от каста
WAVE_S = 15            # волна бурстов рейда: несколько DPS в пределах 15 с
_VULN = re.compile(r"уязвим|vulnerab", re.I)
_TAKEN = re.compile(r"получаем\w* (?:\S+ )?урон|урон, получаем|damage taken|takes? (?:\S+ )?(?:more|increased|additional) damage|"
                    r"получает (?:на \d+% )?больше урона|больше урона", re.I)
_INC = re.compile(r"увелич|повыш|increas|больше|more|additional|amplif", re.I)
_RED = re.compile(r"уменьш|сниж|reduc|less damage|меньше урона|невосприимч|immun|поглощ|absorb", re.I)


def amp_text(text: str | None) -> bool:
    """По тексту способности: босс получает больше урона (уязвимость, «получаемый урон увеличен»).
    Оглушение, щит, «снижает урон» — не в счёт."""
    t = text or ""
    if not t or _RED.search(t):
        return False
    return bool(_VULN.search(t) or (_TAKEN.search(t) and _INC.search(t)))


AMP_RE = re.compile(r"уязвим|vulnerab", re.I)   # совместимость: прежнее имя


def _key(s) -> str:
    return "".join(ch for ch in str(s or "") if ch.isalnum()).lower()


def _table() -> dict[int, dict]:
    return {int(c["id"]): c for c in game_data.dps_cds()}


def _fits(c: dict, p: dict) -> bool:
    """Кулдаун из таблицы — этого класса и спека (спек в таблице не указан — у всего класса)."""
    if c.get("class") and _key(c["class"]) != _key(p.get("cls")):
        return False
    return not (c.get("spec") and p.get("spec") and _key(c["spec"]) != _key(p["spec"]))


def _phase_label(phases: list[dict], t: float) -> tuple[int | None, str]:
    cur = None
    for p in phases or []:
        if p["t"] <= t + 1e-6:
            cur = p
    if not cur or (cur["n"] == 1 and len(phases) < 2):
        return (cur or {}).get("n"), ""
    return cur["n"], f"{cur['name']} +{_fmt_t(max(0.0, t - cur['t']))}"


def _phase_t(phases: list[dict], t: float) -> float:
    cur = None
    for p in phases or []:
        if p["t"] <= t + 1e-6:
            cur = p
    return round(t - (cur["t"] if cur else 0.0), 1)


def _dead_spans(raw, players, rel, dur) -> dict[int, list[tuple[float, float]]]:
    """Когда игрок лежал: от смерти до первого своего каста после неё (боевое воскрешение) или до конца боя."""
    casts = defaultdict(list)
    for ev in raw.get("casts") or []:
        casts[int(ev.get("sourceID", -1))].append(rel(ev))
    for v in casts.values():
        v.sort()
    out = defaultdict(list)
    for ev in raw.get("deaths") or []:
        if ev.get("type") not in (None, "death"):
            continue
        pid = int(ev.get("targetID", -1))
        if pid not in players:
            continue
        td = rel(ev)
        if out[pid] and out[pid][-1][1] > td:
            continue
        back = next((t for t in casts.get(pid, []) if t > td + 2), None)
        out[pid].append((td, back if back is not None else dur))
    return out


def _dead_at(spans, pid, t0, t1=None) -> bool:
    """Лежал в момент t0 (или всё время с t0 по t1)."""
    t1 = t0 if t1 is None else t1
    return any(a <= t0 and t1 <= b for a, b in spans.get(pid, []))


def burst_analysis(raw: dict, players: dict, owner, rel, nm, dur: float, phases: list[dict]) -> dict:
    """{lust, lusts, bursts: [игрок…], externals, windows, timeline, waves, hints} — по кастам игроков боя."""
    table = _table()
    dps = {pid: p for pid, p in players.items() if p.get("role") == "dps"}
    dead = _dead_spans(raw, players, rel, dur)
    lust_casts, ext = [], []
    by_player: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for ev in raw.get("casts") or []:
        if ev.get("type") != "cast":
            continue
        ab, t = int(ev.get("abilityGameID", 0)), rel(ev)
        src = owner(int(ev.get("sourceID", -1)))
        name = nm(ab)
        if ab in LUST_IDS or LUST_RE.search(name):
            if src in players:
                lust_casts.append({"t": round(t, 1), "caster": players[src]["name"], "name": name})
            continue
        c = table.get(ab)
        if not c or src not in players or not _fits(c, players[src]):
            continue
        if c["kind"] == "external":
            tgt = int(ev.get("targetID", -1))
            ext.append({"t": round(t, 1), "time": _fmt_t(t), "name": name, "from": players[src]["name"],
                        "phase": _phase_label(phases, t)[1],
                        "to": players[tgt]["name"] if tgt in players else "—"})
        elif src in dps:
            by_player[src][ab].append(round(t, 1))
    # Бурсты, которые включаются сами, без нажатия (Radiant Glory: «Гнев карателя» от «Испепеляющего следа»):
    # бафф из таблицы на игроке, а каста этой способности у него нет
    procs: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for ev in sorted(raw.get("burst_buffs") or [], key=lambda e: e.get("timestamp", 0)):
        if ev.get("type") != "applybuff":
            continue
        ab, pid = int(ev.get("abilityGameID", 0)), int(ev.get("targetID", -1))
        c = table.get(ab)
        if (not c or c["kind"] != "burst" or pid not in dps or owner(int(ev.get("sourceID", -1))) != pid
                or ab in by_player.get(pid, {}) or not _fits(c, dps[pid])):
            continue
        t = round(rel(ev), 1)
        if not procs[pid][ab] or t - procs[pid][ab][-1] > 2:
            procs[pid][ab].append(t)

    # Героизм: у разных шаманов/магов в один момент — один героизм; второй — через 10 мин (Пресыщение)
    lusts = []
    for x in sorted(lust_casts, key=lambda x: x["t"]):
        if lusts and x["t"] - lusts[-1]["t"] < LUST_SAME_S:
            continue
        n, label = _phase_label(phases, x["t"])
        lusts.append({**x, "time": _fmt_t(x["t"]), "phase": n, "phase_label": label,
                      "phase_t": _phase_t(phases, x["t"]), "pull": x["t"] <= 5})
    lust = lusts[0] if lusts else None

    rows = []
    for pid, p in dps.items():
        alive = max(0.0, dur - sum(b - a for a, b in dead.get(pid, [])))
        cds = []
        for src, proc in ((by_player.get(pid, {}), False), (procs.get(pid, {}), True)):
            for ab, ts in src.items():
                c = table[ab]
                cd = float(c.get("cd") or 120)
                ch = int(c.get("charges") or 1)
                ts = sorted(ts)
                mx = None if proc else max(len(ts), ch + int(alive // cd))
                cds.append({"id": ab, "name": nm(ab) if nm(ab) and not nm(ab).startswith("Spell ") else c["name"],
                            "cd": cd, "charges": ch, "times": ts, "time": ", ".join(_fmt_t(x) for x in ts),
                            "used": len(ts), "max_uses": mx, "proc": proc, "lazy": False})
        cds.sort(key=lambda c: min(c["times"]))
        main = max((c for c in cds if not c["proc"]), key=lambda c: c["cd"], default=None)
        spec_cd = max((float(c.get("cd") or 0) for c in table.values()
                       if c["kind"] == "burst" and c.get("spec") and _fits(c, p)), default=0.0)
        rows.append({"pid": pid, "player": p["name"], "cls": p.get("cls"), "spec": p.get("spec"), "cds": cds,
                     "main": main["id"] if main else None, "spec_cd": spec_cd, "alive_s": round(alive, 1),
                     "dead": [list(x) for x in dead.get(pid, [])], "with_lust": None})
    for r in rows:   # под героизм — главный бурст (самый долгий, от 1,5 мин; иначе любой), если игрок был жив
        if not lusts or not r["cds"]:
            continue
        live = [L for L in lusts if not _dead_at(dead, r["pid"], L["t"])]
        if not live:
            continue
        main = next((c for c in r["cds"] if c["id"] == r["main"]), None)
        pool = [main] if main and main["cd"] >= MAIN_CD_S else [c for c in r["cds"] if not c["proc"]] or r["cds"]
        r["with_lust"] = any(L["t"] - LUST_LEAD_S <= t <= L["t"] + LUST_TAIL_S for L in live for c in pool for t in c["times"])
    rows.sort(key=lambda r: (not r["cds"], r["player"]))

    windows = _boss_windows(raw, players, owner, rel, nm, dur, phases)
    _window_hits(windows, rows, dead)
    waves = [{**w, "time": _fmt_t(w["t"]), "phase_label": _phase_label(phases, w["t"])[1]} for w in _waves(rows)]
    B = {"lust": lust, "lusts": lusts, "bursts": rows, "externals": ext, "windows": windows, "waves": waves,
         "duration": round(dur, 1), "top": None, "site": raw.get("site") or "www",
         "uses": [{"cls": r["cls"], "spec": r["spec"], "id": c["id"], "used": c["used"], "max": c["max_uses"]}
                  for r in rows for c in r["cds"] if not c["proc"] and c["max_uses"]]}
    refresh(B)
    return B


def _window_hits(windows, rows, dead) -> None:
    """Кто из DPS попал бурстом в окно, а у кого главный бурст был готов, но не нажат."""
    for w in windows:
        hit, ready = [], []
        for r in rows:
            if _dead_at(dead, r["pid"], w["t0"]):
                continue
            if any(w["t0"] - WIN_LEAD_S <= t <= w["t1"] for c in r["cds"] for t in c["times"]):
                hit.append(r["player"])
                continue
            main = next((c for c in r["cds"] if c["id"] == r["main"]), None)
            if main is None:
                if not r["cds"] and r["spec_cd"] >= MAIN_CD_S:
                    ready.append(r["player"])   # ни одного бурста за бой — был готов всегда
                continue
            if main["cd"] < MAIN_CD_S or main["proc"]:
                continue
            recent = sum(1 for t in main["times"] if w["t0"] - main["cd"] < t < w["t0"] - WIN_LEAD_S)
            late = any(w["t1"] < t <= w["t1"] + WIN_LATE_S for t in main["times"])
            if recent < main["charges"] and not late:
                ready.append(r["player"])
        w["hit"], w["missed_ready"] = hit, ready
        w["of"] = sum(1 for r in rows if not _dead_at(dead, r["pid"], w["t0"]))


def _amp_list(boss: str | None) -> list[dict]:
    """Механики, под которые жмут бурсты: таблица game_data.json → amp_windows и гайды Mythic Trap этого
    босса (тип «Damage amp» — бить цель, «Shield» — быстро снять щит)."""
    out = []
    for a in game_data.amp_windows():
        if a.get("boss") and boss and _key(a["boss"]) != _key(boss):
            continue
        out.append({"id": a.get("id"), "names": {_key(a.get("name")), _key(a.get("en"))} - {""}, "note": a.get("note")})
    if boss:
        try:
            from .guides import bosses, ru_text
            for _r, b in bosses():
                if _key(b.get("name")) != _key(boss):
                    continue
                seen = set()
                for lst in (b.get("abilities") or {}).values():
                    for a in lst or []:
                        if a.get("type") in ("Damage amp", "Shield") and (a.get("id"), a.get("name")) not in seen:
                            seen.add((a.get("id"), a.get("name")))
                            note = ru_text("todo", a.get("todo")) or ru_text("type", a.get("type"))
                            out.append({"id": a.get("id"), "names": {_key(a.get("name"))} - {""}, "note": note})
        except Exception:  # noqa: BLE001 — гайды необязательны
            pass
    return out


def _amp_match(amps, ab: int, name: str) -> dict | None:
    k = _key(name)
    return next((a for a in amps if (a.get("id") and int(a["id"]) == ab) or (k and k in a["names"])), None)


def _boss_windows(raw, players, owner, rel, nm, dur, phases) -> list[dict]:
    """Баффы и дебаффы на боссе от самой механики боя (не от игроков): окна, когда босс уязвим, без щита и т. п.
    Учитываются обновления (refresh) и то, что висело ещё до пулла."""
    amps = _amp_list(((raw.get("fight") or {}).get("name")))
    evs = sorted((raw.get("boss_debuffs") or []) + (raw.get("boss_buffs") or []), key=lambda e: e["timestamp"])
    open_: dict = {}
    seen: set = set()
    spans = defaultdict(list)
    for ev in evs:
        ab, typ = int(ev.get("abilityGameID", 0)), str(ev.get("type") or "")
        src = owner(int(ev.get("sourceID", -1)))
        if src in players:
            continue
        key = (ab, int(ev.get("targetID", -1)))
        t = rel(ev)
        if typ in ("applydebuff", "applybuff"):
            open_.setdefault(key, t)
        elif typ.startswith(("refresh", "applydebuffstack", "applybuffstack")):
            if key not in open_ and key not in seen:
                open_[key] = 0.0          # висело ещё до пулла
        elif typ in ("removedebuff", "removebuff"):
            if key in open_:
                spans[ab].append((open_.pop(key), t))
            elif key not in seen:
                spans[ab].append((0.0, t))
        seen.add(key)
    for (ab, _tg), t in open_.items():
        spans[ab].append((t, dur))
    # Механика из гайда видна только кастом босса — окно от каста
    for ev in raw.get("boss_casts") or []:
        if ev.get("type") != "cast":
            continue
        ab = int(ev.get("abilityGameID", 0))
        if ab in spans or not _amp_match(amps, ab, nm(ab)):
            continue
        t = rel(ev)
        if not any(a <= t <= b for a, b in spans.get(-ab, [])):
            spans[-ab].append((t, min(dur, t + CAST_WIN_S)))
    out = []
    for ab, iv in spans.items():
        iv = _union(iv)   # один дебафф на двух боссах сразу — одно окно
        if sum(b - a for a, b in iv) > 0.8 * dur:   # висит весь бой — это не окно
            continue
        sid = abs(ab)
        name = nm(sid)
        cur = _amp_match(amps, sid, name)
        for a, b in iv:
            if b - a < MIN_WIN_S:
                continue
            n, label = _phase_label(phases, a)
            out.append({"id": sid, "name": name, "t0": round(a, 1), "t1": round(b, 1), "time": _fmt_t(a),
                        "phase": n, "phase_label": label, "from_cast": ab < 0,
                        "amp": "sure" if cur else ("maybe" if amp_text(name) else None),
                        "note": (cur or {}).get("note"), "desc": None})
    out.sort(key=lambda w: (not w["amp"], w["t0"]))
    return sorted(out[:20], key=lambda w: w["t0"])


def _union(iv: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(iv):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def enrich_windows(B: dict, dur: float, online: bool = True) -> None:
    """Описания окон на боссе (Mythic Trap в программе, иначе Wowhead): «возможно, окно для бурста» — и по
    описанию, не только по названию. Подсказки и таблица по времени пересчитываются."""
    ws = B.get("windows") or []
    if not ws:
        return
    from .guides import spell_ru
    desc = {w["id"]: (spell_ru(w["id"]) or {}).get("desc") for w in ws}
    need = [i for i, d in desc.items() if not d and i > 0]
    if need and online:
        try:
            from . import wowhead
            got = wowhead.lookup(need, limit=10, timeout=3)
            desc.update({i: (got.get(i) or {}).get("desc") for i in need})
        except Exception:  # noqa: BLE001 — описание необязательно
            pass
    for w in ws:
        w["desc"] = desc.get(w["id"]) or None
        if not w["amp"] and amp_text(w["desc"]):
            w["amp"] = "maybe"
    B.setdefault("duration", round(dur, 1))
    refresh(B)


def _waves(rows: list[dict]) -> list[dict]:
    """Моменты, когда сразу несколько DPS жмут бурсты (окно 15 с) — «волны» бурстов рейда."""
    presses = sorted((t, r["player"]) for r in rows for c in r["cds"] if not c["proc"] for t in c["times"])
    out, i = [], 0
    while i < len(presses):
        t0 = presses[i][0]
        grp = [p for p in presses[i:] if p[0] <= t0 + WAVE_S]
        who = list(dict.fromkeys(n for _, n in grp))
        if len(who) >= max(2, len(rows) // 3):
            out.append({"t": t0, "n": len(who), "who": who})
            i += len(grp)
        else:
            i += 1
    return out


def _timeline(B: dict) -> list[dict]:
    rows = B.get("bursts") or []
    out = []
    for L in B.get("lusts") or ([B["lust"]] if B.get("lust") else []):
        out.append({"t": L["t"], "time": L["time"], "phase": L.get("phase_label") or "", "kind": "lust",
                    "what": L["name"] + (" (на пулле)" if L.get("pull") else ""), "who": L["caster"]})
    for w in B.get("windows") or []:
        if not w.get("amp"):
            continue   # в таблицу по времени — только окна, под которые жмут бурсты
        what = f"«{w['name']}» на {_fmt_t(w['t1'] - w['t0'])}"
        what += (f" — {w['note']}" if w.get("note") else " — окно для бурстов") if w["amp"] == "sure" \
            else " — возможно, босс получает больше урона"
        out.append({"t": w["t0"], "time": w["time"], "phase": w.get("phase_label") or "", "kind": "window", "what": what,
                    "who": f"бурст нажали {len(w.get('hit') or [])} из {w.get('of', len(rows))}" if rows else ""})
    for e in B.get("externals") or []:
        out.append({"t": e["t"], "time": e["time"], "phase": e.get("phase") or "", "kind": "external",
                    "what": f"{e['name']} → {e['to']}", "who": e["from"]})
    for wave in B.get("waves") or []:
        out.append({"t": wave["t"], "time": wave["time"], "phase": wave.get("phase_label") or "", "kind": "burst",
                    "what": f"Бурсты: {wave['n']} DPS",
                    "who": ", ".join(wave["who"][:6]) + ("…" if len(wave["who"]) > 6 else "")})
    out.sort(key=lambda e: (e["t"], e["kind"] != "lust"))
    return out


def _list(xs, n=5) -> str:
    return ", ".join(xs[:n]) + ("…" if len(xs) > n else "")


def refresh(B: dict) -> None:
    """Пересчитать подсказки, отметки «нажато реже» и таблицу по времени (после описаний окон и сравнения с топом).
    B["key"] — главная строка для «Главного по бою» (или None)."""
    rows, ws, dur, T = B.get("bursts") or [], B.get("windows") or [], float(B.get("duration") or 0), B.get("top")
    ratios = (T or {}).get("ratios") or {}
    out, keys = [], {}
    lusts = B.get("lusts") or ([B["lust"]] if B.get("lust") else [])
    cmp = compare_lust(B.get("lust"), T)
    if lusts:
        s = "Героизм — " + ", ".join(L["time"] + (f" ({L['phase_label']})" if L.get("phase_label") else "")
                                     for L in lusts) + f", нажал {lusts[0]['caster']}"
        judged = [r for r in rows if r.get("with_lust") is not None]
        if not judged:
            s += ". Бурстов DPS под него не найдено"
        else:
            late = [r["player"] for r in judged if r["with_lust"] is False]
            s += (f". Без главного бурста под героизм: {_list(late)}" if late
                  else ". Все DPS с найденными бурстами прожали их под героизм")
        if cmp:
            s += "; " + cmp[0]
            if cmp[1]:
                keys["lust"] = s
        out.append(s)
    elif rows:
        s = "Героизма в этом бою не было" + (f"; {cmp[0]}" if cmp else "")
        out.append(s)
        if cmp:
            keys["lust"] = s
    wv = compare_waves(B.get("waves"), T, dur)
    if wv:
        out.append(wv)
        keys["waves"] = wv
    retail = (B.get("site") or "www") == "www"
    for w in sorted([w for w in ws if w.get("amp") and retail], key=lambda w: (w["amp"] != "sure", w["t0"]))[:3]:
        if w.get("missed_ready"):
            s = ((f"Окно «{w['name']}»" if w["amp"] == "sure" else f"Возможное окно «{w['name']}»") + f" в {w['time']}"
                 + (f" ({w['phase_label']})" if w.get("phase_label") else "")
                 + f": главный бурст был готов, но не нажат — {_list(w['missed_ready'])}")
            out.append(s)
            if w["amp"] == "sure":
                keys.setdefault("window", s)
    lazy = []
    for r in rows:
        for c in r["cds"]:
            c["lazy"] = False
            if c["proc"] or not c.get("max_uses") or dur <= 120 or not retail:
                continue
            gap = c["max_uses"] - c["used"]
            if gap < 2 or not (c["cd"] >= MAIN_CD_S or c["used"] < 0.7 * c["max_uses"]):
                continue
            ratio = ratios.get(f"{_key(r['cls'])}|{_key(r['spec'])}|{c['id']}")
            if ratio is not None and c["used"] >= int(ratio * c["max_uses"] + 0.5):
                continue   # лучшие игроки спека тоже жмут не каждый откат — у вас не реже
            c["lazy"] = True
            lazy.append((r["player"], c["name"], c["used"], c["max_uses"], ratio))
    if lazy:
        out.append("Бурсты нажаты реже, чем можно (с учётом времени, пока игрок лежал): "
                   + "; ".join(f"{p} — «{n}» {u} из {m}" + (f", у лучших киллов этот спек — {q:.0%} откатов" if q is not None else "")
                               for p, n, u, m, q in lazy[:4]) + ("…" if len(lazy) > 4 else ""))
    if not retail:
        out.append("Таблица бурстов — по основной игре: на этой версии игры откаты другие, поэтому «нажато реже» "
                   "и «бурст был готов» не считаются; видно, когда героизм и кто что нажал")
    none = [r["player"] for r in rows if not r["cds"]]
    if none and len(none) < len(rows):
        out.append(f"Не нашли крупных бурстов у: {_list(none)} — проверьте вручную: спек или таланты могли не попасть "
                   f"в таблицу, а у части талантов бурст включается сам, без нажатия (как Radiant Glory у паладина)")
    B["hints"] = out
    B["key"] = keys.get("waves") or keys.get("window") or keys.get("lust")
    B["timeline"] = _timeline(B)


def _hints(lust, rows, windows, dur) -> list[str]:
    """Совместимость: подсказки без сравнения с топом."""
    B = {"lust": lust, "bursts": rows, "windows": windows, "duration": dur}
    refresh(B)
    return B["hints"]


def top_summary(kills: list[dict]) -> dict | None:
    """Лучшие киллы: когда героизм (фаза и время от её начала), волны бурстов и как часто каждый спек жмёт бурсты.
    kills — из fetch_top_kills (поле burst)."""
    ks = [k for k in kills or [] if k.get("burst")]
    if not ks:
        return None
    lusts = [k["burst"]["lust"] for k in ks if k["burst"].get("lust")]
    out = {"kills": len(ks), "lust": None, "waves": [], "ratios": {}}
    if lusts:
        by_phase = defaultdict(list)
        for x in lusts:
            by_phase[x.get("phase") or 1].append(x)
        ph, xs = max(by_phase.items(), key=lambda kv: len(kv[1]))
        pt = median(x.get("phase_t", x["t"]) for x in xs)
        multi = any((x.get("phase") or 1) > 1 for x in lusts) or ph > 1
        out["lust"] = {"phase": ph, "n": len(xs), "of": len(ks), "time": _fmt_t(median(x["t"] for x in xs)),
                       "phase_t": round(pt, 1), "label": f"фаза {ph} +{_fmt_t(pt)}" if multi else ""}
    for i, k in enumerate(ks):
        ws = k["burst"].get("waves") or []
        out["waves"].append({"guild": k.get("guild"), "kill": i, "duration": k.get("duration"),
                             "times": [w["time"] for w in ws], "t": [w["t"] for w in ws]})
    acc = defaultdict(list)
    for k in ks:
        for u in k["burst"].get("uses") or []:
            if u.get("max"):
                acc[f"{_key(u.get('cls'))}|{_key(u.get('spec'))}|{u['id']}"].append(min(1.0, u["used"] / u["max"]))
    out["ratios"] = {k: round(median(v), 2) for k, v in acc.items() if len(v) >= 2}
    return out


def compare_waves(mine: list[dict] | None, top: dict | None, dur: float | None = None) -> str | None:
    """Волны бурстов, которые делает больше половины лучших киллов (после первых 30 с), а у вас их нет.
    Считаются разные киллы, волны — вокруг одной точки (±15 с), и только те, до которых ваш бой дошёл."""
    if not top or not top.get("waves"):
        return None
    pts = sorted((t, k.get("kill", i)) for i, k in enumerate(top["waves"]) for t in k.get("t") or [] if t > 30)
    need = max(2, (len(top["waves"]) + 1) // 2)
    groups = []
    while pts:
        best = max(pts, key=lambda p: len({k for t, k in pts if abs(t - p[0]) <= WAVE_S}))
        grp = [p for p in pts if abs(p[0] - best[0]) <= WAVE_S]
        if len({k for _, k in grp}) < need:
            break
        groups.append(median(t for t, _ in grp))
        pts = [p for p in pts if p not in grp]
    own = [w["t"] for w in mine or [] if w["t"] > 30]
    missing = sorted(c for c in groups if (dur is None or c + 10 <= dur) and not any(abs(o - c) <= 20 for o in own))
    if not missing:
        return None
    return ("Лучшие киллы делают ещё волны бурстов рейдом: " + ", ".join(_fmt_t(t) for t in missing[:3])
            + " — у вас в это время бурсты почти никто не жал")


def compare_lust(mine: dict | None, top: dict | None) -> tuple[str, bool] | None:
    """(часть строки про героизм, отличается ли от лучших киллов). Сравнивается фаза и время от её начала."""
    if not top or not top.get("lust"):
        return None
    tl = top["lust"]
    where = tl["label"] or tl["time"]
    share = f"{tl['n']} из {tl['of']}"
    if not mine:
        return f"лучшие киллы жмут его в {where} ({share})", True
    if (mine.get("phase") or 1) != tl["phase"]:
        return f"у лучших киллов — в другой фазе: {where} ({share})", True
    diff = float(mine.get("phase_t", mine["t"])) - float(tl.get("phase_t", 0))
    if abs(diff) > 30:
        return (f"у лучших киллов — {where} ({share}): у вас на {_fmt_t(abs(diff))} "
                + ("позже" if diff > 0 else "раньше")), True
    return f"так же, как у лучших киллов ({where})", False

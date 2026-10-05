"""Нанесение урона: когда рейд жмёт героизм, бурсты (крупные боевые кулдауны DPS) и внешние усиления
(Придание сил), когда на боссе окна механик (уязвимость, оглушение…), и совпадает ли одно с другим.

Зеркало плана сейвов: там — входящий урон и сейвы, здесь — исходящий урон и бурсты. Кулдауны — из таблицы
игровых данных (game_data.json → dps_cds, номера проверены на Wowhead). Окна на боссе — дебаффы на боссе,
наложенные не игроками (сама механика боя); «усиливает урон» — по названию или описанию способности.
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
WIN_LEAD_S = 10        # бурст «в окно»: нажат не раньше 10 с до начала окна и до его конца
MIN_WIN_S = 4          # окна короче 4 с — не окна
AMP_RE = re.compile(r"уязвим|vulnerab|exposed|обнаж|получаем\w* урон|damage taken|урон, получаемый|"
                    r"больше урона|more damage|оглуш|stunned|ошеломл|сломл|broken|shatter", re.I)


def _key(s) -> str:
    return "".join(ch for ch in str(s or "") if ch.isalnum()).lower()


def _table() -> dict[int, dict]:
    return {int(c["id"]): c for c in game_data.dps_cds()}


def _phase_label(phases: list[dict], t: float) -> tuple[int | None, str]:
    cur = None
    for p in phases or []:
        if p["t"] <= t + 1e-6:
            cur = p
    if not cur or (cur["n"] == 1 and len(phases) < 2):
        return (cur or {}).get("n"), ""
    return cur["n"], f"{cur['name']} +{_fmt_t(max(0.0, t - cur['t']))}"


def burst_analysis(raw: dict, players: dict, owner, rel, nm, dur: float, phases: list[dict]) -> dict:
    """{lust, bursts: [игрок…], externals, windows, timeline, hints} — по касту каждого игрока боя."""
    table = _table()
    dps = {pid: p for pid, p in players.items() if p.get("role") == "dps"}
    lusts, ext = [], []
    by_player: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for ev in raw.get("casts") or []:
        if ev.get("type") != "cast":
            continue
        ab, t = int(ev.get("abilityGameID", 0)), rel(ev)
        src = owner(int(ev.get("sourceID", -1)))
        name = nm(ab)
        if ab in LUST_IDS or LUST_RE.search(name):
            if src in players:
                lusts.append({"t": round(t, 1), "caster": players[src]["name"], "name": name})
            continue
        c = table.get(ab)
        if not c or src not in players:
            continue
        p = players[src]
        if c.get("class") and _key(c["class"]) != _key(p.get("cls")):
            continue
        if c["kind"] == "external":
            tgt = int(ev.get("targetID", -1))
            ext.append({"t": round(t, 1), "time": _fmt_t(t), "name": name, "from": p["name"],
                        "to": players[tgt]["name"] if tgt in players else "—"})
        elif src in dps:
            by_player[src][ab].append(round(t, 1))
    # героизм: у разных шаманов/магов в один момент — один героизм
    lust = None
    if lusts:
        lusts.sort(key=lambda x: x["t"])
        lust = {**lusts[0], "time": _fmt_t(lusts[0]["t"])}
        lust["phase"], lust["phase_label"] = _phase_label(phases, lust["t"])

    windows = _boss_windows(raw, players, owner, rel, nm, dur, phases)

    rows = []
    for pid, p in dps.items():
        cds = []
        for ab, ts in sorted(by_player.get(pid, {}).items(), key=lambda x: min(x[1])):
            c = table[ab]
            cd = float(c.get("cd") or 120)
            ts = sorted(ts)
            cds.append({"id": ab, "name": nm(ab) if nm(ab) and not nm(ab).startswith("Spell ") else c["name"],
                        "cd": cd, "times": ts, "time": ", ".join(_fmt_t(x) for x in ts),
                        "used": len(ts), "max_uses": int(dur // cd) + 1})
        rows.append({"pid": pid, "player": p["name"], "cls": p.get("cls"), "spec": p.get("spec"), "cds": cds,
                     "with_lust": bool(lust) and any(lust["t"] - LUST_LEAD_S <= t <= lust["t"] + LUST_TAIL_S
                                                     for c in cds for t in c["times"])})
    rows.sort(key=lambda r: (not r["cds"], r["player"]))

    for w in windows:  # кто из DPS попал бурстом в окно, а у кого бурст был готов, но не нажат
        hit, ready = [], []
        for r in rows:
            if any(w["t0"] - WIN_LEAD_S <= t <= w["t1"] for c in r["cds"] for t in c["times"]):
                hit.append(r["player"])
            elif any(all(not (w["t0"] - c["cd"] < t < w["t0"]) for t in c["times"]) for c in r["cds"]):
                ready.append(r["player"])   # главный кулдаун к окну откатан, а нажат не был
        w["hit"], w["missed_ready"] = hit, ready

    waves = [{**w, "time": _fmt_t(w["t"]), "phase_label": _phase_label(phases, w["t"])[1]} for w in _waves(rows)]
    timeline = []
    if lust:
        timeline.append({"t": lust["t"], "time": lust["time"], "phase": lust["phase_label"], "kind": "lust",
                         "what": f"{lust['name']}", "who": lust["caster"]})
    for w in windows:
        timeline.append({"t": w["t0"], "time": w["time"], "phase": w["phase_label"], "kind": "window",
                         "what": f"«{w['name']}» на {_fmt_t(w['t1'] - w['t0'])}"
                                 + (" — усиливает урон по боссу" if w["amp"] else ""),
                         "who": f"бурст нажали {len(w['hit'])} из {len(rows)}" if rows else ""})
    for e in ext:
        timeline.append({"t": e["t"], "time": e["time"], "phase": _phase_label(phases, e["t"])[1], "kind": "external",
                         "what": f"{e['name']} → {e['to']}", "who": e["from"]})
    for wave in waves:
        timeline.append({"t": wave["t"], "time": wave["time"], "phase": wave["phase_label"],
                         "kind": "burst", "what": f"Бурсты: {wave['n']} DPS", "who": ", ".join(wave["who"][:6])
                         + ("…" if len(wave["who"]) > 6 else "")})
    timeline.sort(key=lambda e: (e["t"], e["kind"] != "lust"))

    return {"lust": lust, "bursts": rows, "externals": ext, "windows": windows, "timeline": timeline, "waves": waves,
            "hints": _hints(lust, rows, windows, dur)}


def enrich_windows(B: dict, dur: float, online: bool = True) -> None:
    """Описания окон на боссе (Mythic Trap в программе, иначе Wowhead): «усиливает урон» — и по описанию,
    не только по названию. Подсказки пересчитываются."""
    ws = B.get("windows") or []
    if not ws:
        return
    from .guides import spell_ru
    desc = {w["id"]: (spell_ru(w["id"]) or {}).get("desc") for w in ws}
    need = [i for i, d in desc.items() if not d and i > 0]
    if need and online:
        try:
            from . import wowhead
            got = wowhead.lookup(need, limit=10)
            desc.update({i: (got.get(i) or {}).get("desc") for i in need})
        except Exception:  # noqa: BLE001 — описание необязательно
            pass
    for w in ws:
        w["desc"] = desc.get(w["id"]) or None
        w["amp"] = w["amp"] or bool(w["desc"] and AMP_RE.search(w["desc"]))
    keep = [h for h in B.get("hints") or [] if h.startswith("Героизм:") or h.startswith("Лучшие киллы")]
    B["hints"] = keep + _hints(B.get("lust"), B.get("bursts") or [], ws, dur)
    for e in B.get("timeline") or []:
        if e["kind"] == "window":
            w = next((w for w in ws if w["time"] == e["time"] and w["name"] in e["what"]), None)
            if w and w["amp"] and "усиливает" not in e["what"]:
                e["what"] += " — усиливает урон по боссу"


def _boss_windows(raw, players, owner, rel, nm, dur, phases) -> list[dict]:
    """Дебаффы на боссе от самой механики боя (не от игроков): окна, когда босс уязвим, оглушён и т. п."""
    open_: dict = {}
    spans = defaultdict(list)
    for ev in sorted(raw.get("boss_debuffs") or [], key=lambda e: e["timestamp"]):
        ab, typ = int(ev.get("abilityGameID", 0)), ev.get("type")
        src = owner(int(ev.get("sourceID", -1)))
        if src in players:
            continue
        if typ == "applydebuff":
            open_.setdefault(ab, rel(ev))
        elif typ == "removedebuff" and ab in open_:
            spans[ab].append((open_.pop(ab), rel(ev)))
    for ab, t in open_.items():
        spans[ab].append((t, dur))
    out = []
    for ab, iv in spans.items():
        total = sum(b - a for a, b in iv)
        if total > 0.8 * dur:   # висит весь бой — это не окно
            continue
        for a, b in iv:
            if b - a < MIN_WIN_S:
                continue
            n, label = _phase_label(phases, a)
            out.append({"id": ab, "name": nm(ab), "t0": round(a, 1), "t1": round(b, 1), "time": _fmt_t(a),
                        "phase": n, "phase_label": label, "amp": bool(AMP_RE.search(nm(ab) or "")), "desc": None})
    return sorted(out, key=lambda w: w["t0"])[:20]


def _waves(rows: list[dict]) -> list[dict]:
    """Моменты, когда сразу несколько DPS жмут бурсты (окно 15 с) — «волны» бурстов рейда."""
    presses = sorted((t, r["player"]) for r in rows for c in r["cds"] for t in c["times"])
    out, i = [], 0
    while i < len(presses):
        t0 = presses[i][0]
        grp = [p for p in presses[i:] if p[0] <= t0 + 15]
        who = list(dict.fromkeys(n for _, n in grp))
        if len(who) >= max(2, len(rows) // 3):
            out.append({"t": t0, "n": len(who), "who": who})
            i += len(grp)
        else:
            i += 1
    return out


def _hints(lust, rows, windows, dur) -> list[str]:
    out = []
    if lust:
        late = [r["player"] for r in rows if r["cds"] and not r["with_lust"]]
        out.append(f"Героизм — {lust['time']}" + (f" ({lust['phase_label']})" if lust["phase_label"] else "")
                   + f", нажал {lust['caster']}"
                   + (f". Без бурста под героизм: {', '.join(late[:5])}" + ("…" if len(late) > 5 else "") if late
                      else ". Все DPS прожали бурсты под героизм"))
    elif rows:
        out.append("Героизма в этом бою не было")
    for w in [w for w in windows if w["amp"]][:3]:
        if w["missed_ready"]:
            out.append(f"Окно «{w['name']}» в {w['time']}" + (f" ({w['phase_label']})" if w["phase_label"] else "")
                       + f": бурст был готов, но не нажат — {', '.join(w['missed_ready'][:5])}")
    lazy = [(r["player"], c["name"], c["used"], c["max_uses"]) for r in rows for c in r["cds"]
            if c["max_uses"] - c["used"] >= 2 and dur > 120]
    if lazy:
        out.append("Бурсты нажаты реже, чем можно: " + "; ".join(f"{p} — «{n}» {u} из {m}" for p, n, u, m in lazy[:4])
                   + ("…" if len(lazy) > 4 else ""))
    none = [r["player"] for r in rows if not r["cds"]]
    if none and len(none) < len(rows):
        out.append(f"Не нашли крупных бурстов у: {', '.join(none[:5])} — проверьте вручную (спек или таланты могли не попасть в таблицу)")
    return out


def top_summary(kills: list[dict]) -> dict | None:
    """Лучшие киллы: когда героизм (время и фаза) и когда волны бурстов. kills — из fetch_top_kills (поле burst)."""
    ks = [k for k in kills if k.get("burst")]
    if not ks:
        return None
    lusts = [k["burst"]["lust"] for k in ks if k["burst"].get("lust")]
    out = {"kills": len(ks), "lust": None, "waves": []}
    if lusts:
        by_phase = defaultdict(list)
        for x in lusts:
            by_phase[x.get("phase") or 1].append(x)
        ph, xs = max(by_phase.items(), key=lambda kv: len(kv[1]))
        out["lust"] = {"phase": ph, "n": len(xs), "of": len(ks), "time": _fmt_t(median(x["t"] for x in xs)),
                       "label": next((x["phase_label"] for x in xs if x.get("phase_label")), "")}
    for k in ks:
        out["waves"].append({"guild": k.get("guild"), "times": [w["time"] for w in k["burst"].get("waves") or []],
                             "t": [w["t"] for w in k["burst"].get("waves") or []]})
    return out


def compare_waves(mine: list[dict], top: dict | None) -> str | None:
    """Вторая и следующие волны бурстов у лучших киллов (после первых 30 с), которых нет у вас."""
    if not top or not top.get("waves"):
        return None
    later = [t for k in top["waves"] for t in k.get("t") or [] if t > 30]
    if not later:
        return None
    later.sort()
    groups, cur = [], [later[0]]
    for t in later[1:]:
        if t - cur[-1] <= 20:
            cur.append(t)
        else:
            groups.append(cur)
            cur = [t]
    groups.append(cur)
    need = max(2, (len(top["waves"]) + 1) // 2)
    common = [g for g in groups if len(g) >= need]
    own = [w["t"] for w in mine or [] if w["t"] > 30]
    missing = [median(g) for g in common if not any(abs(o - median(g)) <= 20 for o in own)]
    if not missing:
        return None
    return ("Лучшие киллы делают ещё волны бурстов рейдом: " + ", ".join(_fmt_t(t) for t in missing[:3])
            + " — у вас в это время бурсты почти никто не жал")


def compare_lust(mine: dict | None, top: dict | None) -> str | None:
    if not top or not top.get("lust"):
        return None
    tl = top["lust"]
    where = f"{tl['time']}" + (f", {tl['label'].split(' +')[0]}" if tl.get("label") else "")
    if not mine:
        return f"Лучшие киллы жмут героизм: {where} ({tl['n']} из {tl['of']})"
    if (mine.get("phase") or 1) != tl["phase"]:
        return (f"Героизм: у вас — {mine['time']}" + (f" ({mine['phase_label']})" if mine.get("phase_label") else "")
                + f", у лучших киллов — в другой фазе: {where} ({tl['n']} из {tl['of']})")
    return f"Героизм — в той же фазе, что у лучших киллов ({where})"

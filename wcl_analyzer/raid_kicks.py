"""Прерывания: что враги кастуют и что из этого прерывали, кто прерывал и нажимал впустую,
очередь прерываний на следующий пулл и заметка MRT.

Всё — из уже загруженных данных боя (касты врагов, события прерываний, касты игроков): лишних запросов нет.
Прерываемой считается способность врага, которую в этом бою прервали хотя бы раз.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from . import game_data
from .compare import _fmt_t
from .raid_burst import _key

CAST_WINDOW_S = 15.0    # прерывание или завершение каста — до 15 с от начала (и до следующего каста этого врага)
MULTI_S = 5.0           # касты разных врагов одного типа ближе 5 с — враги кастуют одновременно
MAX_KICKERS = 5         # больше пяти человек в одной очереди — уже нужен стан или контроль, а не очередь
WASTED_MIN = 3          # «прерывание впустую» в подсказках — от трёх нажатий


def _empty(reason: str = "") -> dict:
    return {"groups": [], "players": [], "mrt": "", "hints": [reason] if reason else [], "key": None}


def _table() -> dict[int, dict]:
    return {int(c["id"]): c for c in game_data.interrupts()}


def _default_kick(cls: str, spec: str, table: dict) -> dict | None:
    """Прерывание, которое у этого спека обычно есть (для очереди, если игрок в этом бою не жал)."""
    c, s = _key(cls), _key(spec)
    for x in table.values():
        if x.get("default", True) is False or _key(x["class"]) != c:
            continue
        if not x.get("specs") or s in {_key(v) for v in x["specs"]}:
            return x
    return None


def kick_analysis(raw: dict, players: dict, owner, rel, nm, dur: float, site: str = "www", latin: bool = True) -> dict:
    """players: {id: {name, cls, spec, role}}; owner(id) — хозяин питомца; rel(ev) — секунда боя."""
    table = _table()
    actors = {int(a["id"]): a for a in ((raw.get("report") or {}).get("masterData") or {}).get("actors") or []}

    def kname(c: dict) -> str:
        n = nm(int(c["id"]))
        if n and not n.startswith("Spell "):
            return n   # как в логе
        return (c.get("en") if latin else c.get("name")) or c.get("en") or ""

    # ---------------------------------------------- прерывания: кто, кого и что
    kicks = []          # (t, игрок, враг, способность врага, прерывание)
    for ev in raw.get("interrupts") or []:
        if ev.get("type") != "interrupt":
            continue
        src = owner(int(ev.get("sourceID", -1)))
        ab = int(ev.get("extraAbilityGameID") or 0)
        if src in players and ab:
            kicks.append((rel(ev), src, (int(ev.get("targetID", -1)), int(ev.get("targetInstance") or 0)), ab,
                          int(ev.get("abilityGameID") or 0)))
    # прерываемая — любая способность, которую в бою прервали (в том числе не игрок: питомец без хозяина, NPC)
    kickable = {int(ev.get("extraAbilityGameID") or 0) for ev in raw.get("interrupts") or []
                if ev.get("type") == "interrupt" and ev.get("extraAbilityGameID")}
    # нажатия прерываний игроками (в том числе впустую: каст уже сбит или кончился)
    presses: dict[int, list] = defaultdict(list)
    for ev in raw.get("casts") or []:
        if ev.get("type") != "cast":
            continue
        ab = int(ev.get("abilityGameID", 0))
        src = owner(int(ev.get("sourceID", -1)))
        if ab in table and src in players:
            presses[src].append((rel(ev), ab))
    if not kickable and not presses:
        return _empty("Прерываний в бою не было")

    # ---------------------------------------------- касты врагов: попытки прерываемых способностей
    begins: dict[tuple, list] = defaultdict(list)   # (враг, способность) → начала кастов
    done: dict[tuple, list] = defaultdict(list)     # (враг, способность) → завершённые касты
    for ev in raw.get("boss_casts") or []:
        ab = int(ev.get("abilityGameID", 0))
        if ab not in kickable:
            continue
        # одинаковые адды — один sourceID, разный sourceInstance: каждый — отдельный враг
        key = ((int(ev.get("sourceID", -1)), int(ev.get("sourceInstance") or 0)), ab)
        if ev.get("type") == "begincast":
            begins[key].append(rel(ev))
        elif ev.get("type") == "cast":
            done[key].append(rel(ev))
    kicked_at: dict[tuple, list] = defaultdict(list)
    for t, pid, tgt, ab, _k in kicks:
        kicked_at[(tgt, ab)].append((t, pid))

    groups: dict[tuple, dict] = {}   # (тип врага, способность) → сводка
    for key in set(begins) | set(done) | set(kicked_at):
        (src, _inst), ab = key
        a = actors.get(src) or {}
        gk = (int(a.get("gameID") or src), ab)
        g = groups.setdefault(gk, {"id": ab, "name": nm(ab), "npc": a.get("name") or "—", "attempts": [],
                                   "kicked": 0, "missed": 0, "missed_t": [], "by": Counter(), "spans": []})
        ks = sorted(kicked_at.get(key, []))
        fin = sorted(done.get(key, []))
        starts = sorted(begins.get(key, []))
        if not starts:   # касты без начала (мгновенные или лог без begincast): попытка = прерывание или прошедший каст
            starts = sorted([t for t, _ in ks] + fin)
        used_k, used_f = set(), set()
        if starts:
            g["spans"].append((starts[0], starts[-1], key[0]))
        for i, t in enumerate(starts):
            end = min(starts[i + 1] if i + 1 < len(starts) else t + CAST_WINDOW_S, t + CAST_WINDOW_S) + 0.05
            k = next((j for j, (kt, _) in enumerate(ks) if j not in used_k and t - 0.05 <= kt <= end), None)
            f = next((j for j, ft in enumerate(fin) if j not in used_f and t - 0.05 <= ft <= end), None)
            if k is not None:
                used_k.add(k)
                g["kicked"] += 1
                g["by"][ks[k][1]] += 1
                g["attempts"].append(t)
            elif f is not None:
                used_f.add(f)
                g["missed"] += 1
                g["missed_t"].append(t)
                g["attempts"].append(t)
            # иначе каст сбит станом, отменён или враг погиб — не в счёт
    groups = {k: g for k, g in groups.items() if g["attempts"]}

    # ---------------------------------------------- кто может прерывать и как часто
    kick_of: dict[int, dict] = {}
    cd_of: dict[int, float] = {}
    for pid, p in players.items():
        ps = sorted(presses.get(pid, []))
        c = table.get(Counter(ab for _, ab in ps).most_common(1)[0][0]) if ps else None
        if c is None and site == "www" and p.get("role") != "healer":
            c = _default_kick(p.get("cls", ""), p.get("spec", ""), table)
        if c is None:
            continue
        kick_of[pid] = c
        ts = [t for t, ab in ps if ab == int(c["id"])]
        gaps = [b - a for a, b in zip(ts, ts[1:]) if b - a >= 0.5 * float(c["cd"])]
        cd_of[pid] = min([float(c["cd"])] + gaps)   # жал чаще отката из таблицы — таланты: откат по факту

    # ---------------------------------------------- очередь на следующий пулл
    busy: dict[int, list] = defaultdict(list)   # игрок → секунды, на которые он уже поставлен

    def ready(pid: int, t: float, local: list) -> bool:
        return all(abs(t - x) >= cd_of[pid] for x in busy[pid] + [x for q, x in local if q == pid])

    def simulate(rot: list[int], times: list[float]) -> tuple[list, int]:
        local, miss, pos = [], 0, 0
        for t in times:
            for j in range(len(rot)):
                pid = rot[(pos + j) % len(rot)]
                if ready(pid, t, local):
                    local.append((pid, t))
                    pos = (pos + j + 1) % len(rot)
                    break
            else:
                miss += 1
        return local, miss

    out_groups = []
    load = Counter()
    for gk, g in sorted(groups.items(), key=lambda kv: (-len(kv[1]["attempts"]), kv[1]["name"])):
        times = sorted(g["attempts"])
        # несколько врагов этого типа кастуют одновременно (адды) — на каждого свой человек, общая очередь
        # не годится: в план — те, кто их прерывал, без проверки «успевают ли по кругу»
        sp = sorted(g["spans"])
        # «одновременно» — касты двух разных врагов ближе MULTI_S или их серии кастов перекрываются
        multi = any(b[0] < a[1] or b[0] - a[0] < MULTI_S for a, b in zip(sp, sp[1:]))
        # сначала — кто прерывал эту способность в бою, потом — кто прерывал вообще, потом — у кого прерывание есть
        pref = [pid for pid, _ in g["by"].most_common() if pid in kick_of]
        pref += sorted((pid for pid in kick_of if pid not in pref and presses.get(pid)),
                       key=lambda pid: (load[pid], -len(presses[pid]), players[pid]["name"]))
        pref += sorted((pid for pid in kick_of if pid not in pref),
                       key=lambda pid: (load[pid], players[pid].get("role") == "tank", players[pid]["name"]))
        rot, local, miss = [], [], len(times)
        if multi:
            rot = [pid for pid, _ in g["by"].most_common() if pid in kick_of][:MAX_KICKERS + 1] or pref[:2]
            local, miss = [], 0
        for pid in ([] if multi else pref):
            if miss == 0 or len(rot) >= MAX_KICKERS:
                break
            trial, m = simulate(rot + [pid], times)
            if m < miss or not rot:
                rot, local, miss = rot + [pid], trial, m
        for pid, t in local:
            busy[pid].append(t)
            load[pid] += 1
        backup = next((pid for pid in pref if pid not in rot), None)

        def who(pid):
            p = players[pid]
            return {"player": p["name"], "cls": p.get("cls"), "kick": kname(kick_of[pid]), "kick_id": int(kick_of[pid]["id"]),
                    "cd": round(cd_of[pid], 1)}
        out_groups.append({
            "id": g["id"], "name": g["name"], "npc": g["npc"], "casts": len(times), "kicked": g["kicked"],
            "missed": g["missed"], "missed_times": [_fmt_t(t) for t in g["missed_t"][:8]],
            "by": [{"player": players[pid]["name"], "cls": players[pid].get("cls"), "n": n} for pid, n in g["by"].most_common()],
            "rotation": [who(pid) for pid in rot], "backup": who(backup) if backup is not None else None,
            "uncovered": miss, "multi": multi, "enemies": len(sp),
            "gap": round(min((b - a for a, b in zip(times, times[1:])), default=0.0), 1) if not multi else None})

    # ---------------------------------------------- игроки
    done_by = Counter(k[1] for k in kicks)
    rows = []
    for pid in sorted(set(presses) | set(done_by), key=lambda pid: (-done_by[pid], players[pid]["name"])):
        n_press = len(presses.get(pid, []))
        rows.append({"player": players[pid]["name"], "cls": players[pid].get("cls"), "role": players[pid].get("role"),
                     "kicks": done_by[pid], "presses": max(n_press, done_by[pid]),
                     "wasted": max(0, n_press - done_by[pid]),
                     "kick": kname(kick_of[pid]) if pid in kick_of else ""})

    K = {"groups": out_groups, "players": rows, "site": site,
         "total": sum(g["casts"] for g in out_groups), "kicked": sum(g["kicked"] for g in out_groups),
         "missed": sum(g["missed"] for g in out_groups)}
    K["mrt"] = mrt_kicks(K, (raw.get("fight") or {}).get("name") or "")
    K["hints"], K["key"] = _hints(K)
    return K


def mrt_kicks(K: dict, title: str = "") -> str:
    """Заметка MRT: на каждую прерываемую способность — очередь игроков (по порядку) и запасной."""
    from .raid_top import CLASS_COLOR

    def nm(p):
        c = CLASS_COLOR.get(p.get("cls") or "")
        return f"|cff{c}{p['player']}|r" if c else p["player"]
    lines = []
    for g in K.get("groups") or []:
        if not g["rotation"] or (g["casts"] < 2 and not g["missed"]):
            continue
        # один враг — очередь по порядку (>); несколько сразу — список, каждый на своего
        s = f"{{spell:{g['id']}}} {g['npc']}: " + (", " if g.get("multi") else " > ").join(nm(p) for p in g["rotation"])
        if g.get("backup"):
            s += f" (запас: {nm(g['backup'])})"
        lines.append(s)
    if not lines:
        return ""
    return "\n".join([f"Прерывания: {title}".strip()] + lines)


def _hints(K: dict) -> tuple[list[str], str | None]:
    out = []
    key = None
    for g in K["groups"][:5]:
        rot = " → ".join(p["player"] for p in g["rotation"])
        if g["missed"]:
            s = f"«{g['name']}» ({g['npc']}): прервано {g['kicked']} из {g['casts']}, прошло {g['missed']}"
            if g["missed_times"]:
                s += f" ({', '.join(g['missed_times'][:4])})"
            if rot:
                s += f"; очередь на следующий пулл: {rot}"
            out.append(s)
        if g["multi"] and g["missed"] and rot:
            out[-1] = out[-1].replace("; очередь на следующий пулл:", "; врагов несколько сразу — на каждого свой человек, прерывали:")
        if g["uncovered"]:
            out.append(f"«{g['name']}» ({g['npc']}): касты идут чаще, чем успевают прерывания даже у {len(g['rotation'])} игроков "
                       f"(между кастами {g['gap']} с) — нужны станы, контроль или больше людей на этом враге")
    wasted = [r for r in K["players"] if r["wasted"] >= WASTED_MIN]
    if wasted:
        out.append("Нажато впустую (каст уже сбит другим или закончился): "
                   + ", ".join(f"{r['player']} ×{r['wasted']}" for r in sorted(wasted, key=lambda r: -r["wasted"])[:5]))
    if K["missed"]:
        worst = max(K["groups"], key=lambda g: g["missed"])
        key = (f"Пропущенные прерывания: «{worst['name']}» ×{worst['missed']}"
               + (f" и ещё {K['missed'] - worst['missed']}" if K["missed"] > worst["missed"] else "")
               + " — очередь для MRT во вкладке «Прерывания»")
    if not out and K["groups"]:
        out.append(f"Все прерываемые касты сбиты: {K['kicked']} из {K['total']}")
    return out, key

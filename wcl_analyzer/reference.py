"""Эталон: агрегированный паттерн Top N игроков."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np

from .logs import PlayerLog
from difflib import SequenceMatcher

from .logs import merge_intervals
from .metrics import (COOLDOWN_CATS, LUST_IDS, POTION_RE, ProcInfo, SpellInfo, classify,
                      compute_metrics, detect_procs, med, pct, wilson_lower)


def stat(values) -> dict:
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0, "median": None, "p25": None, "p75": None, "mean": None,
                "min": None, "max": None}
    return {"n": len(vals), "median": med(vals), "p25": pct(vals, 25), "p75": pct(vals, 75),
            "mean": float(np.mean(vals)), "min": float(min(vals)), "max": float(max(vals))}


@dataclass
class Reference:
    logs: list[PlayerLog]
    spells: dict[int, SpellInfo]
    procs: dict[int, ProcInfo]
    main_cd: int | None
    metrics: list[dict] = field(default_factory=list)
    agg: dict = field(default_factory=dict)
    label: str = "топ-25"
    downtime: list = field(default_factory=list)   # общие паузы топа (фазы без цели и т. п.)
    build_note: str = ""                           # как отобраны логи по билду
    noise: dict | None = None                      # шумовой порог находок (leave-one-out)

    @property
    def n(self) -> int:
        return len(self.logs)

    def name_of(self, ab: int) -> str:
        if ab in self.spells:
            return self.spells[ab].name
        for log in self.logs:
            if ab in log.names:
                return log.names[ab]
        return f"Spell {ab}"


def pick_main_cd(logs: list[PlayerLog], spells: dict[int, SpellInfo]) -> int | None:
    """Главный offensive CD = с самым длинным кулдауном среди используемых ≥60% игроков."""
    n = len(logs)
    cands = []
    for s in spells.values():
        if s.category != "offensive" or not s.cd:
            continue
        users = sum(1 for log in logs if any(c.id == s.id for c in log.casts))
        if users / n >= 0.6:
            cands.append((s.cd, users, s.id))
    return max(cands)[2] if cands else None


def build_reference(logs: list[PlayerLog], overrides: dict | None = None,
                    label: str = "топ-25", me: PlayerLog | None = None) -> Reference:
    """me — ваш лог: если известны таланты, эталон строится по игрокам с похожим билдом."""
    if not logs:
        raise ValueError("Нет логов для эталона")
    build_note = ""
    if me is not None and len(logs) > 8:
        logs, build_note = filter_by_build(logs, me)
    spells = classify(logs, overrides)
    procs = detect_procs(logs, spells)
    main_cd = pick_main_cd(logs, spells)
    ref = Reference(logs=logs, spells=spells, procs=procs, main_cd=main_cd, label=label,
                    build_note=build_note)
    first = [compute_metrics(log, spells, procs, main_cd) for log in logs]
    ref.downtime = common_downtime(first, len(logs))
    ref.metrics = ([compute_metrics(log, spells, procs, main_cd, ref.downtime) for log in logs]
                   if ref.downtime else first)
    ref.agg = aggregate(ref)
    return ref


def common_downtime(ms: list[dict], n: int, share: float = 0.6) -> list[tuple[float, float]]:
    """Секунды, когда ≥60% топа одновременно не действуют: цель недоступна, переход фазы."""
    if n < 4:
        return []
    cnt = Counter()
    for m in ms:
        cnt.update(m["idle_mask"])
    secs = sorted(t for t, c in cnt.items() if c >= share * n)
    return merge_intervals([(t - 0.5, t + 1.5) for t in secs]) if secs else []


def filter_by_build(logs: list[PlayerLog], me: PlayerLog, min_keep: int = 8) -> tuple[list[PlayerLog], str]:
    """Оставляет игроков топа с похожими талантами (п. 9). Если данных о талантах нет — всех."""
    if not me.talents or sum(1 for log in logs if log.talents) < min_keep:
        return logs, ""
    mine = set(me.talents)
    sim = []
    for log in logs:
        if not log.talents:
            continue
        other = set(log.talents)
        sim.append((len(mine & other) / max(1, len(mine | other)), log))
    sim.sort(key=lambda x: -x[0])
    close = [log for sc, log in sim if sc >= 0.85]
    if len(close) >= min_keep:
        return close, f"Похожий билд (таланты совпадают ≥85%): {len(close)} из {len(logs)} логов"
    keep = [log for _, log in sim[:max(min_keep, int(len(sim) * 0.6))]]
    avg = float(np.mean([sc for sc, _ in sim[:len(keep)]])) if keep else 0
    return keep, f"Ближайшие по талантам: {len(keep)} из {len(logs)} (совпадение в среднем {avg:.0%})"


def aggregate(ref: Reference) -> dict:
    ms, n = ref.metrics, ref.n
    agg: dict = {}
    agg["dps"] = stat([m["dps"] for m in ms])
    agg["duration"] = stat([m["duration"] for m in ms])
    agg["ilvl"] = stat([m["ilvl"] for m in ms])
    agg["total_casts"] = stat([m["total_casts"] for m in ms])

    # Статистика способностей (п. 8)
    abil = {}
    for ab, info in ref.spells.items():
        counts = [m["casts"].get(ab, 0) for m in ms]
        users = [m for m in ms if m["casts"].get(ab)]
        firsts = [m["cast_times"][ab][0] for m in users]
        lasts = [m["cast_times"][ab][-1] for m in users]
        ivs = [float(np.mean(np.diff(m["cast_times"][ab]))) for m in users
               if len(m["cast_times"][ab]) > 1]
        up = [m["uptime_buff"].get(ab) for m in ms if ab in m["uptime_buff"]]
        abil[ab] = {
            "casts": stat(counts), "cpm": stat([m["cpm"].get(ab, 0.0) for m in ms]),
            "cpm_active": stat([m["cpm_active"].get(ab, 0.0) for m in ms]),
            "users": len(users), "share": len(users) / n,
            "first_use": med(firsts), "last_use": med(lasts), "interval": med(ivs),
            "uptime": med(up) if up else None,
        }
    agg["abilities"] = abil

    # Тайминги кулдаунов: k-е применение
    cd_timing = {}
    for ab, info in ref.spells.items():
        if info.category not in COOLDOWN_CATS:
            continue
        per_k = defaultdict(list)
        for m in ms:
            for k, t in enumerate(m["cast_times"].get(ab, [])):
                per_k[k].append(t)
        rows = []
        for k in sorted(per_k):
            ts = per_k[k]
            if len(ts) < max(2, 0.2 * n):
                continue
            s = stat(ts)
            iqr = (s["p75"] - s["p25"]) if s["p25"] is not None else 0
            near = sum(1 for t in ts if abs(t - s["median"]) <= max(10.0, iqr))
            ph = [phase_rel(m["phases"], m["cast_times"][ab][k]) for m in ms
                  if len(m["cast_times"].get(ab, [])) > k]
            ph = [x for x in ph if x is not None]
            phase = Counter(p for p, _ in ph).most_common(1)[0][0] if ph else None
            offs = [o for p, o in ph if p == phase]
            rows.append({"k": k + 1, **s, "players": len(ts),
                         "support": near, "confidence": wilson_lower(near, n),
                         "times": ts,
                         "phase": phase if len(offs) >= 0.6 * len(ts) and len(set(p for p, _ in ph)) > 1 else None,
                         "phase_off": stat(offs)})
        if rows:
            cd_timing[ab] = rows
    agg["cd_timing"] = cd_timing
    agg["cd_uses"] = {ab: stat([m["cd_usage"][ab]["used"] for m in ms if ab in m["cd_usage"]])
                      for ab in ref.spells if any(ab in m["cd_usage"] for m in ms)}
    agg["cd_delay"] = {ab: stat([m["cd_usage"][ab]["avg_delay"] for m in ms if ab in m["cd_usage"]])
                       for ab in agg["cd_uses"]}

    # Opener: самая частая способность на каждой позиции
    opener = []
    for i in range(12):
        col = [m["opener"][i] for m in ms if len(m["opener"]) > i]
        if not col:
            break
        ab, k = Counter(col).most_common(1)[0]
        times = [ref.logs[j].casts[i].t for j, m in enumerate(ms)
                 if len(m["opener"]) > i and m["opener"][i] == ab]
        opener.append({"pos": i + 1, "id": ab, "share": k / n, "t": med(times),
                       "confidence": wilson_lower(k, n)})
    agg["opener"] = opener
    # Опенер как последовательность: «консенсус» — лог топа, ближе всех к остальным;
    # шум — насколько топы сами отличаются от консенсуса (п. 7)
    seqs = [m["opener_seq"] for m in ms if len(m["opener_seq"]) >= 5]
    if len(seqs) >= 3:
        def dist(a, b):
            return 1 - SequenceMatcher(None, a, b, autojunk=False).ratio()
        best = min(seqs, key=lambda a: sum(dist(a, b) for b in seqs))
        agg["opener_seq"] = {"seq": best, "dist": stat([dist(best, b) for b in seqs if b is not best])}
    else:
        agg["opener_seq"] = None

    # N-граммы
    ngr = {}
    for size in (2, 3, 4):
        total = Counter()
        support = Counter()
        for m in ms:
            c = m["ngrams"][size]
            total.update(c)
            for g, cnt in c.items():
                if cnt >= 2:
                    support[g] += 1
        denom = sum(total.values()) or 1
        ngr[size] = [{"seq": g, "count": cnt, "share": cnt / denom, "players": support[g],
                      "player_share": support[g] / n}
                     for g, cnt in total.most_common(10)]
    agg["ngrams"] = ngr

    agg["gcd"] = {
        "idle_per_action": stat([m["gcd"]["idle_per_action"] for m in ms]),
        "idle_share": stat([m["gcd"]["idle_share"] for m in ms]),
        "gcd": stat([m["gcd"]["gcd"] for m in ms]),
        "idle_by_30s": [med([m["gcd"]["idle_by_30s"][i] for m in ms if len(m["gcd"]["idle_by_30s"]) > i])
                        for i in range(max(len(m["gcd"]["idle_by_30s"]) for m in ms))],
    }

    buff_ids = set().union(*[m["uptime_buff"].keys() for m in ms])
    agg["uptime_buff"] = {ab: stat([m["uptime_buff"].get(ab, 0.0) for m in ms])
                          for ab in buff_ids
                          if sum(1 for m in ms if ab in m["uptime_buff"]) >= 0.5 * n}
    deb_ids = set().union(*[m["uptime_debuff"].keys() for m in ms])
    agg["uptime_debuff"] = {ab: stat([m["uptime_debuff"].get(ab, 0.0) for m in ms])
                            for ab in deb_ids
                            if sum(1 for m in ms if ab in m["uptime_debuff"]) >= 0.5 * n}

    res = [m["resource"] for m in ms if m["resource"]]
    agg["resource"] = ({k: stat([r[k] for r in res]) for k in ("mean", "overcap", "starved", "before_burst")}
                       | {"name": res[0]["name"]}) if res else None

    agg["procs"] = {ab: {k: stat([m["procs"][ab][k] for m in ms if ab in m["procs"]])
                         for k in ("gained", "used", "expired", "refreshed", "reaction")}
                    for ab in ref.procs}

    agg["lust"] = stat([m["lust"] for m in ms])
    agg["lust_share"] = sum(1 for m in ms if m["lust"] is not None) / n
    pots = defaultdict(list)
    for m in ms:
        for k, t in enumerate(m["potion_times"]):
            pots[k].append(t)
    agg["potions"] = [{"k": k + 1, **stat(v), "share": len(v) / n} for k, v in sorted(pots.items())
                      if len(v) >= 0.3 * n]
    agg["prepot_share"] = sum(1 for m in ms if m["prepot"]) / n

    # Паттерн бурста: состав k-го окна
    burst = []
    max_w = max((len(m["burst"]) for m in ms), default=0)
    for w in range(max_w):
        wins = [m["burst"][w] for m in ms if len(m["burst"]) > w]
        if len(wins) < 0.3 * n:
            continue
        comp = defaultdict(list)
        for win in wins:
            seen = set()
            for ab, off in win["components"]:
                if ab not in seen:
                    comp[ab].append(off)
                    seen.add(ab)
        lust_offs = [win["lust_offset"] for win in wins if win["lust_offset"] is not None]
        burst.append({
            "window": w + 1, "t0": stat([win["t0"] for win in wins]), "players": len(wins),
            "components": sorted(
                [{"id": ab, "offset": med(offs), "share": len(offs) / len(wins),
                  "confidence": wilson_lower(len(offs), n)}
                 for ab, offs in comp.items() if len(offs) / len(wins) >= 0.3],
                key=lambda c: c["offset"]),
            "lust_share": len(lust_offs) / len(wins), "lust_offset": med(lust_offs),
        })
    agg["burst"] = burst

    # Дебаффы на боссе (рейдовые, свои, окна уязвимости)
    boss_ids = set().union(*[m["uptime_boss"].keys() for m in ms])
    agg["boss_debuffs"] = {}
    for ab in boss_ids:
        have = [m for m in ms if ab in m["uptime_boss"]]
        if len(have) < 0.5 * n:
            continue
        agg["boss_debuffs"][ab] = {
            "uptime": stat([m["uptime_boss"].get(ab, 0.0) for m in ms]),
            "present": len(have) / n,
            "self_share": sum(1 for m in have if m["boss_self"].get(ab)) / len(have),
            "burst_overlap": stat([m["burst_overlap"][ab] for m in ms if ab in m.get("burst_overlap", {})]),
            "windows": [stat(v) for v in _window_starts(ms, ab)],
        }
    ext_ids = set().union(*[m["externals"].keys() for m in ms])
    agg["externals"] = {ab: {"count": stat([m["externals"].get(ab, 0) for m in ms]),
                             "share": sum(1 for m in ms if m["externals"].get(ab)) / n}
                        for ab in ext_ids
                        if sum(1 for m in ms if m["externals"].get(ab)) >= 0.3 * n
                        and ab not in LUST_IDS and not POTION_RE.search(ref.name_of(ab))}
    agg["adds_share"] = stat([m["adds_share"] for m in ms])
    agg["dps_check"] = stat([m["dps"] / m["dps_calc"] for m in ms if m["dps_calc"]])
    agg["alive"] = stat([m["alive"] / m["duration"] for m in ms])

    # Правила приоритета: в состоянии X топ почти всегда жмёт Y (п. 13)
    rules = []
    states = Counter()
    for m in ms:
        for k, c in m["prio_state"].items():
            if c >= 5:
                states[k] += 1
    for k, players in states.items():
        if players < 0.6 * n:
            continue
        nxt = Counter()
        for m in ms:
            for (kk, ab), c in m["prio_next"].items():
                if kk == k:
                    nxt[ab] += c
        for ab, _ in nxt.most_common(2):
            rates = [m["prio_next"].get((k, ab), 0) / m["prio_state"][k] for m in ms
                     if m["prio_state"].get(k, 0) >= 5]
            st = stat(rates)
            if (st["median"] or 0) >= 0.6:
                rules.append({"state": k, "id": ab, "rate": st, "players": len(rates)})
    agg["prio_rules"] = rules

    # Фазы: есть ли у боя переходы фаз (для таймингов относительно фазы, п. 8)
    agg["has_phases"] = sum(1 for m in ms if len(m["phases"]) >= 2) >= 0.5 * n

    # Механики: боссовые способности по урону игрокам + таймлайн кастов босса
    dmg = Counter()
    fights_hit = Counter()
    for m in ms:
        for ab, hits in m["hits_taken"].items():
            dmg[ab] += sum(a for _, a, _ in hits)
            fights_hit[ab] += 1
    key_mech = [ab for ab, _ in dmg.most_common(8) if fights_hit[ab] >= 0.5 * n]
    mech = {}
    boss_ids = set().union(*[m["boss_timeline"].keys() for m in ms])
    for ab in boss_ids | set(key_mech):
        per_k = defaultdict(list)
        for m in ms:
            src = m["boss_timeline"].get(ab)
            if src is None:
                src = sorted({round(t) for t, _, _ in m["hits_taken"].get(ab, [])})
                src = [t for i, t in enumerate(src) if i == 0 or t - src[i - 1] > 3]
            for k, t in enumerate(src):
                per_k[k].append(t)
        occ = [{"k": k + 1, **stat(v)} for k, v in sorted(per_k.items()) if len(v) >= 0.5 * n]
        if occ:
            mech[ab] = {"occurrences": occ, "damage": dmg.get(ab, 0.0) / n,
                        "hit_share": fights_hit.get(ab, 0) / n, "is_key": ab in key_mech}
    agg["mechanics"] = mech

    # Сейвы: к какой механике привязан defensive
    dl = defaultdict(list)
    for m in ms:
        for d in m["defensive_links"]:
            dl[d["id"]].append(d)
    defs = {}
    for ab, rows in dl.items():
        linked = Counter(r["mechanic"] for r in rows if r["mechanic"] is not None)
        mech_ab, k = linked.most_common(1)[0] if linked else (None, 0)
        offs = [r["offset"] for r in rows if r["mechanic"] == mech_ab]
        users = sum(1 for m in ms if ab in m["cast_times"])
        defs[ab] = {"uses": stat([len(m["cast_times"].get(ab, [])) for m in ms]), "users": users,
                    "mechanic": mech_ab, "offset": med(offs), "offset_p25": pct(offs, 25),
                    "offset_p75": pct(offs, 75), "link_share": k / len(rows) if rows else 0,
                    "hp": med([r["hp"] for r in rows]), "confidence": wilson_lower(users, n)}
    agg["defensives"] = defs
    return agg


def phase_rel(phases: list[tuple[float, int]], t: float) -> tuple[int, float] | None:
    """(номер фазы, секунды от её начала) для момента t."""
    if not phases:
        return None
    cur = None
    for start, pid in phases:
        if start <= t + 0.01:
            cur = (pid, t - start)
    return cur


def _window_starts(ms: list[dict], ab: int, max_k: int = 8) -> list[list[float]]:
    per_k: dict[int, list[float]] = defaultdict(list)
    for m in ms:
        for k, (a, _b) in enumerate(m["boss_windows"].get(ab, [])[:max_k]):
            per_k[k].append(a)
    n = len(ms)
    return [per_k[k] for k in sorted(per_k) if len(per_k[k]) >= 0.5 * n]

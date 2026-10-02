"""Классификация способностей и метрики одного игрока."""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .logs import PlayerLog, merge_intervals

from . import game_data as _gd  # noqa: E402

LUST_IDS = _gd.LazyIds(_gd.lust_ids)  # способности жажды крови — из таблицы игровых данных
LUST_RE = re.compile(r"bloodlust|heroism|time warp|primal rage|fury of the aspects|drums|"
                     r"жажда крови|героизм|искажение времени", re.I)
POTION_RE = re.compile(r"potion|зелье", re.I)
HEALTHSTONE_RE = re.compile(r"healthstone|камень здоровья", re.I)
RACIAL_RE = re.compile(r"^(berserking|blood fury|fireblood|ancestral call|arcane torrent|"
                       r"bag of tricks|light's judgment|war stomp|stoneform|gift of the naaru|"
                       r"shadowmeld|rocket barrage|azerite surge|hyper organic light originator|"
                       r"берсерк|кровавое неистовство)$", re.I)

CATEGORY_RU = {
    "rotation": "Ротация", "offensive": "Атакующий кулдаун", "defensive": "Защитный",
    "utility": "Вспомогательный кулдаун", "potion": "Зелье", "healthstone": "Камень здоровья",
    "racial": "Расовая", "trinket": "Тринкет", "lust": "Жажда крови",
}
COOLDOWN_CATS = {"offensive", "defensive", "utility", "potion", "healthstone", "racial", "trinket"}
OFF_GCD_CATS = {"potion", "healthstone", "racial", "trinket"}


@dataclass
class SpellInfo:
    id: int
    name: str
    category: str
    cd: float | None = None
    source: str = "эвристика"
    charges: int = 1
    channel: float | None = None   # длительность канала/занятости после каста, с


@dataclass
class ProcInfo:
    id: int
    name: str
    consumer: int
    consumer_name: str


# ------------------------------------------------------------ statistics
def pct(values, q: float) -> float | None:
    vals = [v for v in values if v is not None]
    return float(np.percentile(vals, q)) if vals else None


def med(values) -> float | None:
    return pct(values, 50)


def wilson_lower(k: int, n: int, z: float = 1.96) -> float:
    """Нижняя граница 95% доверительного интервала Уилсона для доли k/n."""
    if n == 0:
        return 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def union_len(iv: list[tuple[float, float]], lo: float = 0.0, hi: float = 1e12) -> float:
    return sum(max(0.0, min(e, hi) - max(s, lo)) for s, e in merge_intervals(iv))


# -------------------------------------------------------- classification
def load_overrides(path: str | Path | None) -> dict:
    """spell_meta.json: {"<id или имя>": "offensive" | {"category": ..., "cd": 120}}."""
    if not path or not Path(path).exists():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = {}
    for k, v in data.items():
        if k.startswith("_"):
            continue
        out[k.lower()] = v if isinstance(v, dict) else {"category": v}
    return out


def classify(logs: list[PlayerLog], overrides: dict | None = None) -> dict[int, SpellInfo]:
    overrides = overrides or {}
    n = len(logs)
    names: dict[int, str] = {}
    users: Counter = Counter()
    intervals: dict[int, list[float]] = defaultdict(list)
    first_use: dict[int, list[float]] = defaultdict(list)
    trinket_users: Counter = Counter()
    for log in logs:
        names.update(log.names)
        trinket_users.update(getattr(log, "trinket_spells", None) or ())
        seen = defaultdict(list)
        for c in log.casts:
            seen[c.id].append(c.t)
        for ab, ts in seen.items():
            users[ab] += 1
            first_use[ab].append(ts[0])
            intervals[ab].extend(np.diff(ts).tolist())

    spells: dict[int, SpellInfo] = {}
    for ab in users:
        name = names.get(ab, f"Spell {ab}")
        share = users[ab] / n
        iv = intervals[ab]
        cd_est = pct(iv, 5) if len(iv) >= 3 else None
        charges = 1
        p50 = pct(iv, 50) if len(iv) >= 4 else None
        if p50 and p50 >= 25 and cd_est is not None and cd_est < 0.25 * p50:
            # Заряды: часть применений подряд, затем долгая пауза на перезарядку двух зарядов
            short = sum(1 for x in iv if x < 0.25 * p50) / len(iv)
            if short >= 0.15:
                pairs = [a + b for a, b in zip(iv, iv[1:])]
                charges, cd_est = 2, (pct(pairs, 5) or p50) / 2
        cat, src = "rotation", "эвристика"
        if ab in LUST_IDS or LUST_RE.search(name):
            cat, src = "lust", "имя"
        elif POTION_RE.search(name):
            cat, src = "potion", "имя"
        elif HEALTHSTONE_RE.search(name):
            cat, src = "healthstone", "имя"
        elif RACIAL_RE.match(name.strip()):
            cat, src = "racial", "имя"
        elif trinket_users[ab] >= max(1, users[ab] / 2):
            cat, src = "trinket", "экипировка"
        elif cd_est is not None and cd_est >= 25 and share >= 0.3:
            first = med(first_use[ab])
            if first is not None and first <= 20:
                cat = "offensive"
            elif _defensive_link_share(logs, ab) >= 0.5:
                cat = "defensive"
            else:
                cat = "utility"
        elif (cd_est is None or cd_est >= 25) and share < 0.3 and _defensive_link_share(logs, ab) >= 0.6:
            cat = "defensive"
        info = SpellInfo(ab, name, cat, cd_est if cat in COOLDOWN_CATS else None, src,
                         charges=charges if cat in COOLDOWN_CATS else 1)
        ov = overrides.get(str(ab)) or overrides.get(name.lower())
        if ov:
            info.category = ov.get("category", info.category)
            info.cd = float(ov["cd"]) if ov.get("cd") else (info.cd or cd_est)
            info.charges = int(ov.get("charges", info.charges) or 1)
            if ov.get("channel"):
                info.channel = float(ov["channel"])
            info.source = "spell_meta"
        spells[ab] = info
    _detect_channels(logs, spells)
    return spells


def _detect_channels(logs: list[PlayerLog], spells: dict[int, SpellInfo]) -> None:
    """Каналы: после мгновенного «каста» игрок стабильно не действует дольше обычного GCD.
    Такой разрыв — не простой, а время канала; длительность берётся по 20-му перцентилю."""
    after: dict[int, list[float]] = defaultdict(list)
    for log in logs:
        acts = [c for c in log.casts if spells.get(c.id) is None or spells[c.id].category not in OFF_GCD_CATS]
        for a, b in zip(acts, acts[1:]):
            if a.start == a.t:
                after[a.id].append(b.start - a.t)
    base = [pct(v, 20) for v in after.values() if len(v) >= 10]
    base = [x for x in base if x is not None]
    if not base:
        return
    typical = float(np.median(base))
    for ab, v in after.items():
        if len(v) < 10 or ab not in spells or spells[ab].channel:
            continue
        p20 = pct(v, 20)
        if p20 is not None and p20 >= max(1.6 * typical, typical + 0.8):
            spells[ab].channel = p20


def _defensive_link_share(logs: list[PlayerLog], ab: int) -> float:
    """Доля применений, за которыми в 8 с последовал крупный входящий удар."""
    hits = total = 0
    for log in logs:
        amounts = [a for _, _, a, _ in log.dmg_taken]
        if not amounts:
            continue
        big = pct(amounts, 90) or 0
        for c in log.casts:
            if c.id != ab:
                continue
            total += 1
            if any(c.t < t <= c.t + 8 and a >= big for t, _, a, _ in log.dmg_taken):
                hits += 1
    return hits / total if total else 0.0


def detect_procs(logs: list[PlayerLog], spells: dict[int, SpellInfo]) -> dict[int, ProcInfo]:
    """Прок = частый бафф на себе, который снимается кастом одной и той же способности."""
    gains: Counter = Counter()
    consumed_by: dict[int, Counter] = defaultdict(Counter)
    removes: Counter = Counter()
    names: dict[int, str] = {}
    cast_ids = {s.id for s in spells.values() if s.category in COOLDOWN_CATS | {"lust"}}
    for log in logs:
        names.update(log.names)
        casts = sorted((c.t, c.id) for c in log.casts)
        times = [t for t, _ in casts]
        for t, typ, ab, src in log.buff_events:
            if src != log.actor_id or ab in cast_ids:
                continue
            if typ in ("applybuff", "applybuffstack", "refreshbuff"):
                gains[ab] += 1
            elif typ in ("removebuff", "removebuffstack"):
                removes[ab] += 1
                c = _cast_near(casts, times, t)
                if c is not None and c != ab:
                    consumed_by[ab][c] += 1
    procs = {}
    n = max(1, len(logs))
    for ab, g in gains.items():
        if g / n < 4 or not consumed_by[ab]:
            continue
        consumer, k = consumed_by[ab].most_common(1)[0]
        if k / max(1, removes[ab]) >= 0.5:
            procs[ab] = ProcInfo(ab, names.get(ab, f"Spell {ab}"), consumer,
                                 names.get(consumer, f"Spell {consumer}"))
    return procs


def _cast_near(casts, times, t, before=0.3, after=0.05):
    import bisect
    i = bisect.bisect_left(times, t - before)
    best = None
    while i < len(casts) and casts[i][0] <= t + after:
        best = casts[i][1]
        i += 1
    return best


# ------------------------------------------------------------- metrics
def dead_intervals(log: PlayerLog) -> list[tuple[float, float]]:
    """Время после смерти: до первого каста после неё (боевое воскрешение) или до конца боя."""
    out = []
    times = sorted(c.t for c in log.casts)
    import bisect
    for td in log.deaths:
        i = bisect.bisect_right(times, td + 1.0)
        out.append((td, times[i] if i < len(times) else log.duration))
    return merge_intervals(out)


def _overlap(s: float, e: float, iv: list[tuple[float, float]]) -> float:
    return sum(max(0.0, min(e, b) - max(s, a)) for a, b in iv)


def gcd_analysis(log: PlayerLog, spells: dict[int, SpellInfo],
                 excluded: list[tuple[float, float]] | None = None) -> dict:
    """Простой между действиями. Не считаются: время смерти, общие для топа паузы
    (фазы без цели, переходы), каналы. GCD оценивается локально — с учётом хасты."""
    excluded = merge_intervals(list(excluded or []))
    acts = [c for c in log.casts if spells.get(c.id, SpellInfo(c.id, "", "rotation")).category
            not in OFF_GCD_CATS]
    inst = [(a.t, b.start - a.t) for a, b in zip(acts, acts[1:])
            if a.start == a.t and 0.5 < b.start - a.t < 2.0
            and not (spells.get(a.id) and spells[a.id].channel)]
    gcd_global = min(1.5, max(0.75, pct([g for _, g in inst], 20) or 1.2))
    # Локальный GCD по 30-секундным окнам: хаста меняется (Bloodlust, баффы)
    local: dict[int, list[float]] = defaultdict(list)
    for t, g in inst:
        local[int(t // 30)].append(g)
    local_gcd = {b: min(1.5, max(0.6, pct(v, 20))) for b, v in local.items() if len(v) >= 6}
    idles, long_gaps = [], []
    per_bucket: dict[int, float] = defaultdict(float)
    excluded_total = 0.0
    for a, b in zip(acts, acts[1:]):
        gcd = local_gcd.get(int(a.t // 30), gcd_global)
        ch = spells[a.id].channel if spells.get(a.id) else None
        busy_end = max(a.t, a.start + gcd, a.t + ch if ch else 0.0)
        idle = max(0.0, b.start - busy_end)
        if idle and excluded:
            cut = _overlap(busy_end, b.start, excluded)
            excluded_total += cut
            idle = max(0.0, idle - cut)
        idles.append(idle)
        per_bucket[int(b.start // 30)] += idle
        if idle >= 2.0:
            long_gaps.append({"from": busy_end, "to": b.start, "idle": idle,
                              "prev": a.id, "next": b.id})
    long_gaps.sort(key=lambda g: -g["idle"])
    span = max(1.0, log.duration - union_len(excluded, 0, log.duration))
    n_buckets = int(log.duration // 30) + 1
    return {
        "gcd": gcd_global,
        "idle_per_action": float(np.mean(idles)) if idles else 0.0,
        "idle_total": float(sum(idles)),
        "idle_share": float(sum(idles)) / span,
        "actions": len(acts),
        "long_gaps": long_gaps,
        "idle_by_30s": [per_bucket.get(i, 0.0) for i in range(n_buckets)],
        "excluded": excluded_total,
    }


def idle_mask(log: PlayerLog, spells: dict[int, SpellInfo]) -> set[int]:
    """Секунды боя, в которые игрок не действовал ≥2 с подряд — для поиска общих пауз топа."""
    acts = [c for c in log.casts if spells.get(c.id, SpellInfo(c.id, "", "rotation")).category
            not in OFF_GCD_CATS]
    out: set[int] = set()
    for a, b in zip(acts, acts[1:]):
        if b.start - a.t >= 2.5:
            out.update(range(int(a.t) + 1, int(b.start)))
    return out


def cooldown_usage(times: list[float], cd: float, duration: float, charges: int = 1) -> dict:
    """Доступные/использованные применения и задержки: сколько секунд кулдаун простоял
    полностью готовым (все заряды накоплены) перед каждым нажатием."""
    charges = max(1, int(charges or 1))
    ideal = int(max(0.0, duration - 5) // cd) + charges if cd else len(times)
    stock, next_ready, full_since = charges, None, 0.0
    delays = []

    def advance(t):
        nonlocal stock, next_ready, full_since
        while stock < charges and next_ready is not None and next_ready <= t:
            stock += 1
            if stock < charges:
                next_ready += cd
            else:
                full_since, next_ready = next_ready, None

    for t in sorted(times):
        if cd:
            advance(t)
        delays.append(max(0.0, t - full_since) if stock == charges else 0.0)
        if stock >= 1:
            stock -= 1
            if next_ready is None and cd:
                next_ready = t + cd
    end = max(0.0, duration - 5)
    if cd:
        advance(end)
    extra = stock + (int((end - next_ready) // cd) + 1 if cd and next_ready is not None and next_ready <= end else 0)
    return {"used": len(times), "ideal": ideal, "possible_left": extra,
            "missed": max(0, ideal - len(times)),
            "avg_delay": float(np.mean(delays)) if delays else None,
            "max_delay": float(max(delays)) if delays else None}


def compute_metrics(log: PlayerLog, spells: dict[int, SpellInfo],
                    procs: dict[int, ProcInfo], main_cd: int | None,
                    downtime: list[tuple[float, float]] | None = None) -> dict:
    full = log.duration or 1.0
    dead = dead_intervals(log)
    alive = max(1.0, full - union_len(dead, 0, full))
    m: dict = {"duration": full, "alive": alive, "dead": dead, "dps": log.dps, "ilvl": log.ilvl,
               "dps_calc": log.dps_calc}
    counts = Counter(c.id for c in log.casts)
    m["casts"] = dict(counts)
    m["cpm"] = {k: v / (alive / 60) for k, v in counts.items()}
    m["total_casts"] = len(log.casts)
    by_id: dict[int, list[float]] = defaultdict(list)
    for c in log.casts:
        by_id[c.id].append(c.t)
    m["cast_times"] = dict(by_id)

    # Зелья: касты + пре-пот (бафф зелья активен на пулле без каста)
    pot_ids = {s.id for s in spells.values() if s.category == "potion"}
    potion_times = sorted(t for ab in pot_ids for t in by_id.get(ab, []))
    for ab, iv in log.buffs.items():
        if POTION_RE.search(log.name_of(ab)) and iv and iv[0][0] <= 0.5 and not any(t < 2 for t in potion_times):
            potion_times.insert(0, 0.0)
    m["potion_times"] = potion_times
    m["prepot"] = bool(potion_times and potion_times[0] == 0.0 and not any(
        t < 0.5 for ab in pot_ids for t in by_id.get(ab, [])))

    lust = [iv[0][0] for ab, iv in log.buffs.items()
            if iv and (ab in LUST_IDS or LUST_RE.search(log.name_of(ab)))]
    m["lust"] = min(lust) if lust else None

    m["cd_usage"] = {}
    for ab, info in spells.items():
        if info.category in COOLDOWN_CATS - {"potion", "healthstone"} and info.cd:
            m["cd_usage"][ab] = cooldown_usage(by_id.get(ab, []), info.cd, alive, info.charges)

    excl = merge_intervals(list(dead) + list(downtime or []))
    m["gcd"] = gcd_analysis(log, spells, excl)
    m["idle_mask"] = idle_mask(log, spells)
    active_min = max(0.5, (alive - m["gcd"]["idle_total"] - union_len(downtime or [], 0, full)) / 60)
    m["active_min"] = active_min
    m["cpm_active"] = {k: v / active_min for k, v in counts.items()}

    def up(iv):  # доля живого времени
        return max(0.0, union_len(iv, 0, full) - sum(_overlap(a, b, iv) for a, b in dead)) / alive

    m["uptime_buff"] = {ab: up(iv) for ab, iv in log.buffs.items()}
    m["uptime_debuff"] = {ab: up(iv) for ab, iv in log.debuffs.items()}
    # Дебаффы на боссе: доля всего боя; своё ли это (наложено самим игроком)
    m["uptime_boss"] = {ab: union_len(iv, 0, full) / full for ab, iv in log.boss_debuffs.items()}
    m["boss_self"] = {ab: log.actor_id in src for ab, src in log.boss_debuff_src.items()}
    m["boss_windows"] = {ab: iv for ab, iv in log.boss_debuffs.items()}

    # Внешние баффы от других игроков (Power Infusion и т. п.), без Bloodlust
    ext = Counter()
    for t, typ, ab, src in log.buff_events:
        if typ == "applybuff" and src not in (log.actor_id, -1) and src > 0 \
                and ab not in LUST_IDS and not LUST_RE.search(log.name_of(ab)):
            ext[ab] += 1
    m["externals"] = dict(ext)

    # Профиль целей: доля кастов не по боссу
    tot_t = sum(log.cast_targets.values())
    m["adds_share"] = (sum(v for k, v in log.cast_targets.items() if k not in log.boss_ids) / tot_t
                       if tot_t and log.boss_ids else None)

    res = [c.res for c in log.casts if c.res]
    if res:
        p = [a / mx for a, mx, _ in res]
        # overcap: ресурс у предела и после следующего каста всё ещё у предела —
        # то есть прирост между ними потерян (каст-спендер на пределе не считается)
        capped = [a >= 0.95 and b >= 0.95 for a, b in zip(p, p[1:])]
        m["resource"] = {"name": log.resource_name, "mean": float(np.mean(p)),
                         "overcap": float(np.mean(capped)) if capped else 0.0,
                         "starved": float(np.mean([x <= 0.05 for x in p])),
                         "before_burst": _resource_before(log, main_cd),
                         "waste": (log.res_waste / log.res_gain) if log.res_gain else None}
    elif log.res_gain:
        m["resource"] = {"name": log.resource_name or "Ресурс", "mean": None, "overcap": None,
                         "starved": None, "before_burst": None, "waste": log.res_waste / log.res_gain}
    else:
        m["resource"] = None

    m["procs"] = {ab: _proc_stats(log, pr) for ab, pr in procs.items()}

    m["burst"] = []
    burst_iv = []
    if main_cd:
        comp_ids = {s.id for s in spells.values()
                    if s.category in ("offensive", "potion", "racial", "trinket") and s.id != main_cd}
        win = log.buffs.get(main_cd)
        for t0 in by_id.get(main_cd, []):
            comps = [(ab, round(t - t0, 1)) for ab in comp_ids for t in by_id.get(ab, [])
                     if t0 - 10 <= t <= t0 + 20]
            if 0.0 in potion_times and t0 < 10 and not any(ab in pot_ids for ab, _ in comps):
                comps.append((next(iter(pot_ids), -1), round(-t0, 1)))
            lust_off = round(m["lust"] - t0, 1) if m["lust"] is not None and abs(m["lust"] - t0) <= 20 else None
            m["burst"].append({"t0": t0, "components": sorted(comps, key=lambda x: x[1]),
                               "lust_offset": lust_off})
            w = next(((a, b) for a, b in (win or []) if a - 1 <= t0 <= b), None)
            burst_iv.append(w or (t0, t0 + 20))
    # Совпадение окон главного CD с дебаффами на боссе (окна уязвимости, рейдовые дебаффы)
    blen = sum(b - a for a, b in burst_iv)
    m["burst_overlap"] = ({ab: sum(_overlap(a, b, iv) for a, b in burst_iv) / blen
                           for ab, iv in log.boss_debuffs.items()} if blen else {})

    m["defensive_links"] = []
    for ab, info in spells.items():
        if info.category not in ("defensive", "healthstone"):
            continue
        for t in by_id.get(ab, []):
            nxt = [(tt, mech, a) for tt, mech, a, _ in log.dmg_taken if t < tt <= t + 8]
            big = max(nxt, key=lambda x: x[2]) if nxt else None
            hp = next((c.hp for c in log.casts if c.id == ab and abs(c.t - t) < 0.01), None)
            m["defensive_links"].append({
                "id": ab, "t": t, "hp": hp,
                "mechanic": big[1] if big else None,
                "offset": round(big[0] - t, 1) if big else None,
                "hit": big[2] if big else None,
                "taken_5s_before": sum(a for tt, _, a, _ in log.dmg_taken if t - 5 <= tt <= t),
                "taken_5s_after": sum(a for tt, _, a, _ in log.dmg_taken if t < tt <= t + 5),
            })

    boss = defaultdict(list)
    for t, ab in log.boss_casts:
        boss[ab].append(t)
    m["boss_timeline"] = dict(boss)
    m["hits_taken"] = defaultdict(list)
    for t, ab, a, hp in log.dmg_taken:
        m["hits_taken"][ab].append((t, a, hp))
    m["phases"] = list(log.phases)

    rot = [c for c in log.casts if spells.get(c.id) and spells[c.id].category in ("rotation", "offensive")]
    stream = [c.id for c in rot]
    m["opener"] = [c.id for c in log.casts[:12]]
    m["opener_seq"] = stream[:15]
    m["ngrams"] = {n: Counter(tuple(stream[i:i + n]) for i in range(len(stream) - n + 1))
                   for n in (2, 3, 4)}
    m["prio_state"], m["prio_next"] = _priority_states(log, rot, procs)

    if log.dmg_timeline:
        buckets = defaultdict(float)
        for t, a in log.dmg_timeline:
            buckets[int(t // 5)] += a
        m["dps_5s"] = [buckets.get(i, 0.0) / 5 for i in range(int(full // 5) + 1)]
    else:
        m["dps_5s"] = None

    m["deaths"] = []
    for td in log.deaths:
        window = [(ab, a) for t, ab, a, _ in log.dmg_taken if td - 5 <= t <= td]
        by_ab = Counter()
        for ab, a in window:
            by_ab[ab] += a
        defs = [ab for ab, info in spells.items() if info.category in ("defensive", "healthstone")
                for t in by_id.get(ab, []) if td - 15 <= t <= td]
        m["deaths"].append({"t": td, "sources": by_ab.most_common(3), "defensives_15s": defs})
    return m


def _priority_states(log: PlayerLog, rot: list, procs: dict) -> tuple[Counter, Counter]:
    """Состояния перед кастом: активный прок / ресурс у предела или на нуле.
    Возвращает (сколько раз было состояние, сколько раз в нём нажата способность)."""
    state, nxt = Counter(), Counter()
    proc_iv = {}
    for ab in procs:
        proc_iv[ab] = log.buffs.get(ab, [])
    for c in rot:
        keys = []
        for ab, iv in proc_iv.items():
            if any(a + 0.3 <= c.start <= b for a, b in iv):
                keys.append(("buff", ab))
        if c.res:
            frac = c.res[0] / c.res[1]
            if frac >= 0.8:
                keys.append(("res_hi", 0))
            elif frac <= 0.2:
                keys.append(("res_lo", 0))
        for k in keys:
            state[k] += 1
            nxt[(k, c.id)] += 1
    return state, nxt


def _resource_before(log: PlayerLog, main_cd: int | None) -> float | None:
    if not main_cd:
        return None
    vals = []
    casts = [c for c in log.casts if c.res]
    for c in log.casts:
        if c.id == main_cd:
            prev = [x for x in casts if c.t - 3 <= x.t <= c.t]
            if prev:
                a, mx, _ = prev[-1].res
                vals.append(a / mx)
    return float(np.mean(vals)) if vals else None


def _proc_stats(log: PlayerLog, pr: ProcInfo) -> dict:
    casts = sorted((c.t, c.id) for c in log.casts)
    times = [t for t, _ in casts]
    gained = used = expired = refreshed = 0
    last_gain = None
    reactions = []
    for t, typ, ab, src in log.buff_events:
        if ab != pr.id:
            continue
        if typ in ("applybuff", "applybuffstack"):
            gained += 1
            last_gain = t if last_gain is None else last_gain
        elif typ == "refreshbuff":
            gained += 1
            refreshed += 1
        elif typ in ("removebuff", "removebuffstack"):
            if _cast_near(casts, times, t) == pr.consumer:
                used += 1
                if last_gain is not None:
                    reactions.append(t - last_gain)
            elif typ == "removebuff":
                expired += 1
            last_gain = None if typ == "removebuff" else t
    return {"gained": gained, "used": used, "expired": expired, "refreshed": refreshed,
            "reaction": float(np.median(reactions)) if reactions else None}

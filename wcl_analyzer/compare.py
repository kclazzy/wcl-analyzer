"""Compare My Log: сравнение лога игрока с эталоном Top N.

Принципы оценки:
- всё нормируется на время, когда игрок был жив; смерть — отдельная находка со своей ценой;
- влияние на DPS считается как упущенная выгода (замена на филлер), без двойного счёта
  простоя, проков и недостающих кастов;
- шумовой порог: те же проверки прогоняются для каждого игрока топа против остальных
  (leave-one-out). Отклонения, обычные и для топа, в выжимку не попадают.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import numpy as np

from .logs import PlayerLog, merge_intervals
from .metrics import CATEGORY_RU, compute_metrics, med, pct
from .reference import Reference, aggregate, phase_rel

LEVELS = ["LOW", "MEDIUM", "HIGH"]


@dataclass
class Finding:
    section: str
    title: str
    my: float | None = None
    ref: float | None = None
    ref_p25: float | None = None
    ref_p75: float | None = None
    unit: str = ""
    impact: float | None = None      # доля моего DPS, которую оцениваемо теряем
    note: str = ""
    focus: str = ""                  # формулировка для плана тренировок
    severity: float = 0.0            # нормированное отклонение для сортировки Unknown
    evidence: str = ""
    key: str = ""                    # устойчивый ключ проверки (для шумового порога)
    raw_impact: float | None = None  # влияние до вычета шума
    noise: bool = False              # такое же отклонение обычно и у игроков топа
    context: bool = False            # не зависит от игрока (состав рейда, внешние баффы)

    @property
    def diff(self) -> float | None:
        if self.my is None or self.ref is None:
            return None
        return self.my - self.ref

    @property
    def impact_level(self) -> str:
        if self.impact is None:
            return "Unknown"
        if self.impact >= 0.03:
            return "HIGH"
        if self.impact >= 0.01:
            return "MEDIUM"
        return "LOW"


@dataclass
class CompareResult:
    me: PlayerLog
    mm: dict
    ref: Reference
    findings: list[Finding] = field(default_factory=list)
    top5: list[Finding] = field(default_factory=list)
    plan: list[dict] = field(default_factory=list)
    gap: list[dict] = field(default_factory=list)
    reliability: dict = field(default_factory=dict)
    summary: list[dict] = field(default_factory=list)
    tables: dict = field(default_factory=dict)
    timeline: list[dict] = field(default_factory=list)
    brief: dict = field(default_factory=dict)
    progress_keys: dict = field(default_factory=dict)


def _sev(my, s) -> float:
    if my is None or s.get("median") is None:
        return 0.0
    iqr = max((s["p75"] or 0) - (s["p25"] or 0), abs(s["median"]) * 0.05, 1e-6)
    return min(10.0, abs(my - s["median"]) / iqr)


def _fmt_t(t: float | None) -> str:
    if t is None:
        return "—"
    t = int(round(max(0.0, t)))
    return f"{t // 60}:{t % 60:02d}"


def compare(me: PlayerLog, ref: Reference, calibrate: bool = True) -> CompareResult:
    mm = compute_metrics(me, ref.spells, ref.procs, ref.main_cd, ref.downtime)
    r = CompareResult(me=me, mm=mm, ref=ref)
    r.findings, r.tables, offsets = _collect(me, mm, ref)
    if calibrate and ref.n >= 5:
        if ref.noise is None:
            ref.noise = calibrate_noise(ref)
        _apply_noise(r.findings, ref.noise)
    _cap_impacts(r)
    my_dps = max(me.dps, 1.0)
    r.gap = _gap(r, my_dps)
    r.reliability = _reliability(me, ref, mm, offsets)
    r.summary = _summary(r)
    r.top5 = _top5(r.findings)
    r.plan = _plan(r.top5)
    r.timeline = _timeline(r)
    r.brief = _brief(r)
    r.progress_keys = {f.key: {"title": f.title, "impact": f.impact} for f in r.findings
                       if f.key and not f.noise and not f.context}
    return r


# --------------------------------------------------------- шумовой порог
def calibrate_noise(ref: Reference) -> dict:
    """Каждый игрок топа сравнивается с остальными: какие «находки» есть у самих лучших.
    Возвращает {ключ: {impact: p80 влияния, sev: p80 отклонения, rate: доля топа с находкой}}."""
    per_key: dict[str, list[Finding]] = defaultdict(list)
    n = ref.n
    for i in range(n):
        idx = [j for j in range(n) if j != i]
        sub = Reference(logs=[ref.logs[j] for j in idx], spells=ref.spells, procs=ref.procs,
                        main_cd=ref.main_cd, metrics=[ref.metrics[j] for j in idx], label=ref.label,
                        downtime=ref.downtime)
        sub.agg = aggregate(sub)
        fs, _, _ = _collect(ref.logs[i], ref.metrics[i], sub)
        for f in fs:
            if f.key:
                per_key[f.key].append(f)
    out = {}
    for key, fs in per_key.items():
        imps = [f.impact or 0.0 for f in fs] + [0.0] * (n - len(fs))
        out[key] = {"impact": pct(imps, 80) or 0.0, "sev": pct([f.severity for f in fs], 80) or 0.0,
                    "rate": len(fs) / n}
    return out


def _apply_noise(findings: list[Finding], noise: dict) -> None:
    for f in findings:
        nz = noise.get(f.key)
        if not nz or f.context:
            continue
        f.raw_impact = f.impact
        if f.impact is not None:
            f.impact = max(0.0, f.impact - nz["impact"])
        if nz["rate"] >= 0.3 and f.severity <= nz["sev"] * 1.15 and not f.impact:
            f.noise = True
        elif f.raw_impact and f.impact is not None and f.impact < 0.003:
            f.noise = True
    for f in findings:  # оценённое влияние меньше 0,3% — в выжимку не выносим
        if f.impact is not None and f.impact < 0.003 and f.section != "Смерти":
            f.noise = True


def _cap_impacts(r: CompareResult) -> None:
    """Сумма оценок не может объяснять больше реального разрыва с медианой топа (+20%)."""
    my_dps = max(r.me.dps, 1.0)
    gap = ((r.ref.agg["dps"]["median"] or 0) - r.me.dps) / my_dps
    qs = [f for f in r.findings if f.impact and not f.noise and not f.context]
    tot = sum(f.impact for f in qs)
    limit = max(gap, 0.0) * 1.2 + 0.01
    if tot > limit and tot > 0:
        k = limit / tot
        for f in qs:
            f.impact *= k
        r.tables["impact_scaled"] = k


# ------------------------------------------------------------ находки
def _collect(me: PlayerLog, mm: dict, ref: Reference) -> tuple[list[Finding], dict, list[float]]:
    findings: list[Finding] = []
    tables: dict = {}
    agg, name = ref.agg, ref.name_of
    dur, alive, my_dps = me.duration, mm["alive"], max(me.dps, 1.0)
    ref_logs = ref.logs
    add = findings.append

    def dpc(ab: int) -> float | None:
        """Урон за каст: мой, иначе медиана эталона."""
        casts = mm["casts"].get(ab, 0)
        if casts and me.dmg_by_ability.get(ab):
            return me.dmg_by_ability[ab] / casts
        vals = [log.dmg_by_ability.get(ab, 0) / m["casts"][ab]
                for log, m in zip(ref_logs, ref.metrics)
                if m["casts"].get(ab) and log.dmg_by_ability.get(ab)]
        return med(vals) if vals else None

    # Филлер — самая частая ротационная способность: на неё уходит время, если не нажата другая
    rot_ids = [ab for ab, a in agg["abilities"].items()
               if ref.spells[ab].category == "rotation" and a["share"] >= 0.5]
    filler = max(rot_ids, key=lambda ab: agg["abilities"][ab]["casts"]["median"] or 0, default=None)
    filler_dpc = (dpc(filler) or 0.0) if filler else 0.0

    def gain(ab: int) -> float | None:
        d = dpc(ab)
        return None if d is None else max(0.0, d - filler_dpc)

    # Потерянные сверх эталона проки — чтобы не считать их дважды в ротации
    proc_extra: dict[int, float] = {}
    for ab, pr in ref.procs.items():
        mine = mm["procs"].get(ab, {"expired": 0, "refreshed": 0})
        a = agg["procs"][ab]
        extra = (mine["expired"] + mine["refreshed"]) - ((a["expired"]["median"] or 0) + (a["refreshed"]["median"] or 0))
        proc_extra[ab] = max(0.0, extra)

    # ---------------------------------------------------------- смерть
    deaths = []
    for dth in mm["deaths"]:
        killer = dth["sources"][0][0] if dth["sources"] else None
        share = _ref_defensive_share(ref, killer, dth["t"], mm) if killer else None
        deaths.append({"t": dth["t"], "sources": [(name(a), v) for a, v in dth["sources"]],
                       "defensives": [name(a) for a in dth["defensives_15s"]], "ref_def_share": share})
    if mm["deaths"]:
        dead_s = dur - alive
        dth = mm["deaths"][0]
        killer = dth["sources"][0][0] if dth["sources"] else None
        add(Finding(
            "Смерти", f"Смерть в {_fmt_t(dth['t'])}" + (f" от «{name(killer)}»" if killer else "")
            + (f": мёртвым {dead_s:.0f} с ({dead_s / dur:.0%} боя)" if dead_s >= 1 else ""),
            my=dead_s, unit="с без урона", impact=min(1.5, dur / alive - 1) if dead_s >= 1 else None,
            severity=6, key="death",
            note=("За 15 с до смерти защитные способности не нажимались. " if not dth["defensives_15s"] else "")
            + (f"У эталона перед этой механикой защиту нажимали {deaths[0]['ref_def_share']:.0%} игроков. "
               if deaths[0]["ref_def_share"] is not None else "")
            + "Остальные показатели посчитаны только по времени, когда вы были живы.",
            focus=f"Пережить «{name(killer)}»: защитная способность перед ударом" if killer else "Выживание",
            evidence=me.url))
    tables["deaths"] = deaths

    # ---------------------------------------------------------- касты
    cast_rows = []
    k_min = alive / 60
    for ab, a in sorted(agg["abilities"].items(), key=lambda kv: -(kv[1]["casts"]["median"] or 0)):
        info = ref.spells[ab]
        my_n = mm["casts"].get(ab, 0)
        if a["share"] < 0.3 and not my_n:
            continue
        cast_rows.append({"id": ab, "name": info.name, "cat": CATEGORY_RU[info.category],
                          "my": my_n, "my_cpm": my_n / k_min, "ref": a["cpm"]["median"] * k_min,
                          "p25": a["cpm"]["p25"] * k_min, "p75": a["cpm"]["p75"] * k_min, "share": a["share"],
                          "ref_cpm": a["cpm"]["median"]})
        if info.category != "rotation" or a["share"] < 0.5 or ab == filler:
            continue
        # Сравнение на минуту активного времени: простой и смерть сюда не попадают
        ref_rate, my_rate = a["cpm_active"], mm["cpm_active"].get(ab, 0.0)
        if my_rate < (ref_rate["p25"] or 0) * 0.98:
            missing = ((ref_rate["median"] or 0) - my_rate) * mm["active_min"]
            from_procs = sum(proc_extra.get(pab, 0) for pab, pr in ref.procs.items() if pr.consumer == ab)
            missing_own = max(0.0, missing - from_procs)
            g = gain(ab)
            impact = missing_own * g / dur / my_dps if g is not None else None
            target = (ref_rate["median"] or 0) * mm["active_min"]
            add(Finding(
                "Ротация", f"«{info.name}»: меньше кастов, чем у большинства игроков {ref.label}",
                my=my_n, ref=target, ref_p25=(ref_rate["p25"] or 0) * mm["active_min"],
                ref_p75=(ref_rate["p75"] or 0) * mm["active_min"],
                unit="кастов", impact=impact, severity=_sev(my_rate, ref_rate), key=f"rot:{ab}",
                note=("Не использовалась ни разу. " if not my_n else "")
                + "Сравнение на минуту активной игры: простой и время после смерти не учитываются."
                + (f" Из недостающих кастов {from_procs:.0f} — из-за потерянных проков." if from_procs >= 1 else "")
                + (f" Влияние — урон сверх способности «{name(filler)}», которую вы нажимали вместо неё." if impact else ""),
                focus=f"«{info.name}»: ≈ {_n(target, 'каст', 'каста', 'кастов')} за бой (у вас {my_n})",
                evidence=me.url))
    tables["casts"] = cast_rows

    # ------------------------------------------------------------- GCD
    g, gref = mm["gcd"], agg["gcd"]
    lost = max(0.0, g["idle_share"] - (gref["idle_share"]["median"] or 0))
    if g["idle_share"] > (gref["idle_share"]["p75"] or 0) and lost >= 0.005:
        add(Finding(
            "Простой", "Больше простоя между действиями, чем у эталона",
            my=g["idle_share"], ref=gref["idle_share"]["median"],
            ref_p25=gref["idle_share"]["p25"], ref_p75=gref["idle_share"]["p75"],
            unit="доля времени", impact=lost, severity=_sev(g["idle_share"], gref["idle_share"]), key="gcd",
            note=f"Простой {g['idle_total']:.0f} с ({_pct(g['idle_share'])} времени). Не считаются: время смерти, "
                 "общие для топа паузы (фазы без цели), каналы; учтена скорость произнесения (хаста).",
            focus=f"Непрерывный каст: простой ≤ {_pct(gref['idle_share']['median'])} времени "
                  f"(у вас {_pct(g['idle_share'])})",
            evidence=me.url))
    worst = []
    for gap in g["long_gaps"][:5]:
        b = int(gap["to"] // 30)
        ref_b = gref["idle_by_30s"][b] if b < len(gref["idle_by_30s"]) else None
        worst.append({**gap, "prev_name": name(gap["prev"]), "next_name": name(gap["next"]),
                      "ref_bucket_idle": ref_b})
    tables["gcd_worst"] = worst

    # ------------------------------------------------------- кулдауны
    cd_rows, timing_rows = [], []
    uplift = _cd_uplift(me, mm, ref.main_cd)
    holds = _hold_reasons(ref)
    for ab, uses in agg["cd_uses"].items():
        info = ref.spells[ab]
        if info.category not in ("offensive", "defensive", "utility", "racial", "trinket"):
            continue
        a = agg["abilities"].get(ab)
        if a and a["share"] < 0.5:
            continue
        mine = mm["cd_usage"].get(ab) or {"used": 0, "ideal": int(max(0, alive - 5) // info.cd) + 1,
                                           "missed": None, "avg_delay": None, "max_delay": None}
        ref_delay = agg["cd_delay"][ab]["median"]
        if info.category == "defensive":
            mine = {**mine, "avg_delay": None, "max_delay": None}
            ref_delay = None
        cd_rows.append({"id": ab, "name": info.name, "cat": CATEGORY_RU[info.category],
                        "cd": info.cd, "ideal": mine["ideal"], "used": mine["used"],
                        "ref_used": uses["median"], "missed": max(0, mine["ideal"] - mine["used"]),
                        "avg_delay": mine["avg_delay"], "max_delay": mine["max_delay"],
                        "ref_delay": ref_delay, "charges": info.charges})
        # Ожидаемое число применений эталона, пересчитанное на моё живое время
        exp_uses = (uses["median"] or 0) * min(1.0, alive / max(1.0, agg["duration"]["median"] or alive))
        short = exp_uses - mine["used"]
        if short >= 1 and info.category in ("offensive", "racial", "trinket"):
            imp = None
            if uplift and ab == ref.main_cd:
                imp = short * uplift["gain_per_use"] / dur / my_dps
            hold = holds.get(ab)
            add(Finding(
                "Кулдауны", f"«{info.name}»: {_n(mine['used'], 'применение', 'применения', 'применений')} против {exp_uses:.0f} у эталона",
                my=mine["used"], ref=exp_uses, ref_p25=uses["p25"], ref_p75=uses["p75"],
                unit="применений", impact=imp, severity=2 + short, key=f"cdn:{ab}",
                note=f"Доступно при использовании по готовности: {mine['ideal']}"
                     + (f" (заряды: {info.charges})" if info.charges > 1 else "") + ". "
                     f"Средняя задержка: {_c((mine['avg_delay'] or 0))} с (эталон {_c((ref_delay or 0))} с)."
                     + (f" Топ придерживает его под {hold}." if hold else "")
                     + (" Оценка влияния — по приросту вашего DPS в окнах этого CD." if imp else ""),
                focus=f"{info.name}: " + (f"под {hold}, " if hold else "по готовности, ")
                      + f"цель {exp_uses:.0f} применений",
                evidence=me.url))
        elif short >= 1 and info.category == "defensive":
            add(Finding(
                "Защита", f"{info.name}: {mine['used']} применений против {exp_uses:.0f} у эталона",
                my=mine["used"], ref=exp_uses, unit="применений", severity=1.5 + short, key=f"defn:{ab}",
                note="Влияние на DPS не оценивается: защитные способности влияют на выживание.",
                focus=f"{info.name}: использовать {exp_uses:.0f} раз(а) за бой на опасные механики",
                evidence=me.url))

    for ab, rows in agg["cd_timing"].items():
        info = ref.spells[ab]
        if info.category not in ("offensive", "racial", "trinket"):
            continue
        my_ts = mm["cast_times"].get(ab, [])
        off = []
        for row in rows:
            k = row["k"] - 1
            my_t = my_ts[k] if k < len(my_ts) else None
            timing_rows.append({"id": ab, "name": info.name, "k": row["k"], "my": my_t,
                                "ref": row["median"], "p25": row["p25"], "p75": row["p75"],
                                "players": row["players"], "confidence": row["confidence"]})
            if my_t is None or any(a <= my_t <= b for a, b in mm["dead"]):
                continue
            # Тайминг относительно начала фазы, если топ привязан к фазе (п. 8)
            ph = phase_rel(mm["phases"], my_t) if row.get("phase") is not None else None
            if ph and ph[0] == row["phase"] and row["phase_off"]["median"] is not None:
                po = row["phase_off"]
                diff = ph[1] - po["median"]
                iqr = (po["p75"] - po["p25"]) if po["p25"] is not None else 5.0
                rel_note = f" от начала фазы {row['phase']}"
            else:
                diff = my_t - row["median"]
                iqr = (row["p75"] - row["p25"]) if row["p25"] is not None else 5.0
                rel_note = ""
            tol = max(5.0, 2 * iqr)
            if abs(diff) > tol:
                off.append((row, diff, tol, rel_note))
        if off:
            row, diff, tol, rel_note = max(off, key=lambda x: abs(x[1]))
            late = sum(1 for _, d, _, _ in off if d > 0)
            hold = holds.get(ab)
            add(Finding(
                "Кулдауны", f"«{info.name}»: {len(off)} из {len(rows)} применений не в то время, что у эталона "
                            f"(№{row['k']} {'позже' if diff > 0 else 'раньше'} на {abs(diff):.0f} с{rel_note})",
                my=my_ts[row["k"] - 1], ref=row["median"], ref_p25=row["p25"], ref_p75=row["p75"],
                unit="с", severity=min(10, abs(diff) / tol) + len(off) / 2, key=f"cdt:{ab}",
                note=f"Позже: {late}, раньше: {len(off) - late}. Допуск ±{tol:.0f} с. "
                     f"Тайминг №{row['k']} подтверждают {row['support']}/{ref.n} игроков "
                     f"(доверие {row['confidence']:.0%})." + (f" Топ нажимает его под {hold}." if hold else ""),
                focus=f"{info.name} №{row['k']} в {_fmt_t(row['median'])}" + (f" ({hold})" if hold else "")
                      + f", у вас {_fmt_t(my_ts[row['k'] - 1])}",
                evidence=me.url))

    pot_name = next((sp.name for sp in ref.spells.values() if sp.category == "potion"), "Зелье")
    for row in agg["potions"]:
        k = row["k"] - 1
        my_t = mm["potion_times"][k] if k < len(mm["potion_times"]) else None
        timing_rows.append({"id": -1, "name": pot_name, "k": row["k"], "my": my_t, "ref": row["median"],
                            "p25": row["p25"], "p75": row["p75"], "players": row["n"],
                            "confidence": row["share"]})
        if row["median"] is not None and row["median"] > alive:
            continue  # до этого зелья вы не дожили — это учтено в находке о смерти
        if my_t is None or abs(my_t - row["median"]) > max(5.0, 2 * ((row["p75"] or 0) - (row["p25"] or 0))):
            label = "до пулла (пре-пот)" if row["k"] == 1 and agg["prepot_share"] >= 0.5 else f"№{row['k']}"
            add(Finding(
                "Зелья", f"{pot_name} {label}: " + ("не использовано" if my_t is None else
                                                    (f"выпито на {_fmt_t(my_t)}, у эталона — до пулла" if row["median"] < 1 else f"выпито на {_fmt_t(my_t)}, у эталона — на {_fmt_t(row['median'])}")),
                my=my_t, ref=row["median"], unit="с", severity=3, key=f"pot:{row['k']}",
                note=f"Так делают {row['share']:.0%} игроков эталона"
                     + (f"; пре-пот — {agg['prepot_share']:.0%}." if row["k"] == 1 else "."),
                focus=f"{pot_name}: " + ("выпить за 1–2 с до пулла" if label.startswith("до пулла")
                                         else f"в {_fmt_t(row['median'])} вместе с бурстом"),
                evidence=me.url))
    tables["cooldowns"] = cd_rows
    tables["cd_timing"] = timing_rows

    # ------------------------------------------------------------ бурст
    burst_rows = []
    if agg["burst"] and ref.main_cd:
        for w_ref in agg["burst"][:3]:
            w = w_ref["window"] - 1
            my_win = mm["burst"][w] if w < len(mm["burst"]) else None
            my_comp = {}
            if my_win:
                for ab, off in my_win["components"]:
                    my_comp.setdefault(ab, off)
            burst_rows.append({"window": w_ref["window"], "id": ref.main_cd, "name": name(ref.main_cd),
                               "ref_offset": 0.0, "my_offset": 0.0 if my_win else None,
                               "share": 1.0, "ref_t0": w_ref["t0"]["median"],
                               "my_t0": my_win["t0"] if my_win else None})
            misaligned = []
            for c in w_ref["components"]:
                if c["share"] < 0.5:
                    continue
                mo = my_comp.get(c["id"])
                burst_rows.append({"window": w_ref["window"], "id": c["id"], "name": name(c["id"]),
                                   "ref_offset": c["offset"], "my_offset": mo, "share": c["share"],
                                   "ref_t0": w_ref["t0"]["median"], "my_t0": my_win["t0"] if my_win else None})
                if mo is None or abs(mo - c["offset"]) > 3:
                    misaligned.append(name(c["id"]))
            if my_win and misaligned:
                add(Finding(
                    "Бурст", f"Окно бурста №{w_ref['window']}: в него не попали {', '.join(misaligned)}",
                    my=len(misaligned), ref=0, unit="компонентов вне окна ±3 с", severity=2 + len(misaligned),
                    key=f"burst:{w_ref['window']}",
                    note=f"У большинства игроков эталона эти действия идут вместе с кулдауном «{name(ref.main_cd)}».",
                    focus="Собрать окно бурста: " + " → ".join(
                        [name(ref.main_cd)] + [f"{name(c['id'])} ({_sec(c['offset'])})"
                                               for c in w_ref["components"] if c["share"] >= 0.5]),
                    evidence=me.url))
    tables["burst"] = burst_rows

    # ----------------------------------------------- дебаффы на боссе
    boss_rows = []
    for ab, d in sorted(agg["boss_debuffs"].items(), key=lambda kv: -(kv[1]["uptime"]["median"] or 0)):
        up = d["uptime"]
        my_up = mm["uptime_boss"].get(ab, 0.0)
        kind = ("свой" if d["self_share"] >= 0.6 else
                "окно уязвимости" if (up["median"] or 0) < 0.5 else "рейдовый")
        my_ov = mm["burst_overlap"].get(ab) if mm["burst_overlap"] else None
        boss_rows.append({"id": ab, "name": name(ab), "kind": kind, "my": my_up, "ref": up["median"],
                          "p25": up["p25"], "p75": up["p75"], "my_overlap": my_ov,
                          "ref_overlap": d["burst_overlap"]["median"]})
        if kind == "рейдовый" and my_up < (up["p25"] or 0) - 0.15:
            add(Finding(
                "Контекст", f"На боссе почти не было «{name(ab)}»: {my_up:.0%} против {up['median']:.0%} у топа",
                my=my_up, ref=up["median"], unit="доля боя", severity=2, key=f"raiddebuff:{ab}", context=True,
                note="Дебафф вешает другой игрок рейда. Это не ваша ошибка, но урон ниже, чем мог бы быть.",
                focus="", evidence=me.url))
        elif kind == "окно уязвимости" and ref.main_cd and my_ov is not None:
            ov = d["burst_overlap"]
            # Топ специально совмещает бурст с окном: совпадение заметно выше доли окна в бою
            if (ov["median"] or 0) >= 1.5 * (up["median"] or 0) and my_ov < (ov["p25"] or 0) - 0.1:
                starts = ", ".join(_fmt_t(w["median"]) for w in d["windows"][:4])
                add(Finding(
                    "Бурст", f"«{name(ref.main_cd)}» мимо окон «{name(ab)}»: в окне {my_ov:.0%} времени бурста "
                             f"против {ov['median']:.0%} у топа",
                    my=my_ov, ref=ov["median"], ref_p25=ov["p25"], ref_p75=ov["p75"], unit="доля бурста",
                    severity=3 + 5 * ((ov["median"] or 0) - my_ov), key=f"vuln:{ab}",
                    note=f"«{name(ab)}» — дебафф на боссе, во время которого топ держит бурст"
                         + (f" (окна около {starts})" if starts else "") + ".",
                    focus=f"«{name(ref.main_cd)}» под окно «{name(ab)}»" + (f": {starts}" if starts else ""),
                    evidence=me.url))
    tables["boss_debuffs"] = boss_rows

    # ------------------------------------------------- внешние баффы
    ext_rows = []
    for ab, e in agg["externals"].items():
        mine = mm["externals"].get(ab, 0)
        ext_rows.append({"id": ab, "name": name(ab), "my": mine, "ref": e["count"]["median"], "share": e["share"]})
        if e["share"] >= 0.5 and mine < (e["count"]["median"] or 0) - 0.5:
            add(Finding(
                "Контекст", f"Внешний бафф «{name(ab)}»: {mine} против {e['count']['median']:.0f} у топа",
                my=mine, ref=e["count"]["median"], unit="раз", severity=1.5, key=f"ext:{ab}", context=True,
                note=f"Его дают другие игроки; у {e['share']:.0%} топа он есть. Часть разницы в DPS — отсюда.",
                evidence=me.url))
    tables["externals"] = ext_rows

    # ---------------------------------------------- дефенсивы (51, 58)
    def_rows = []
    def_ids = {ab for ab, sp in ref.spells.items() if sp.category in ("defensive", "healthstone")}
    for ab, d in agg["defensives"].items():
        if d["users"] < 0.3 * ref.n or d["mechanic"] is None:
            continue
        mech_name = name(d["mechanic"])
        mine = [x for x in mm["defensive_links"] if x["id"] == ab]
        mech_hits = _occurrences(mm["hits_taken"].get(d["mechanic"], []))
        for k, (t_hit, amount, hp_after) in enumerate(mech_hits):
            if any(a <= t_hit <= b for a, b in mm["dead"]):
                continue
            covered = [x for x in mine if t_hit - 10 <= x["t"] <= t_hit + 10]
            my_lead = round(t_hit - covered[0]["t"], 1) if covered else None
            def_rows.append({"id": ab, "name": name(ab), "mechanic": mech_name,
                             "k": k + 1, "t_hit": t_hit, "my_def_t": covered[0]["t"] if covered else None,
                             "my_lead": my_lead, "ref_lead": d["offset"], "ref_share": d["link_share"],
                             "hp_after": hp_after, "hit": amount})
            if d["link_share"] < 0.5:
                continue
            if my_lead is not None and my_lead < 0:
                add(Finding(
                    "Защита", f"«{name(ab)}»: нажатие через {-my_lead:.0f} с после удара «{mech_name}» №{k + 1}",
                    my=my_lead, ref=d["offset"], ref_p25=d["offset_p25"], ref_p75=d["offset_p75"],
                    unit="с до удара", severity=4, key=f"deflate:{ab}",
                    note=f"У эталона «{name(ab)}» обычно за {_c(d['offset'])} с до удара «{mech_name}» "
                         f"({d['link_share']:.0%} применений)."
                         + (f" Здоровье после удара: {hp_after:.0f}%." if hp_after is not None else ""),
                    focus=f"«{name(ab)}» за {d['offset']:.0f} с до удара «{mech_name}»", evidence=me.url))
            elif my_lead is None:
                any_def = any(t_hit - 8 <= t <= t_hit for a2 in def_ids for t in mm["cast_times"].get(a2, []))
                if not any_def:
                    add(Finding(
                        "Защита", f"Удар «{mech_name}» №{k + 1} ({_fmt_t(t_hit)}) принят без защиты",
                        my=hp_after, unit="% здоровья после удара", severity=4 + (50 - (hp_after or 50)) / 10,
                        key=f"defmiss:{d['mechanic']}",
                        note=f"У эталона на этот удар нажимают «{name(ab)}» за {_c(d['offset'])} с до него "
                             f"({d['link_share']:.0%} применений).",
                        focus=f"«{name(ab)}» за {d['offset']:.0f} с до удара «{mech_name}»", evidence=me.url))
    tables["defensives"] = def_rows

    # --------------------------------------------------------- uptime
    up_rows = []
    for kind, src, my_src in (("Дебафф", agg["uptime_debuff"], mm["uptime_debuff"]),
                              ("Бафф", agg["uptime_buff"], mm["uptime_buff"])):
        for ab, st in src.items():
            if (st["median"] or 0) < 0.15 or ref.spells.get(ab) and ref.spells[ab].category in ("potion", "lust"):
                continue
            if ab in agg["externals"] or ab in agg["boss_debuffs"] and agg["boss_debuffs"][ab]["self_share"] < 0.6:
                continue  # чужие эффекты — в контексте
            my_up = my_src.get(ab, 0.0)
            up_rows.append({"id": ab, "name": name(ab), "kind": kind, "my": my_up, "ref": st["median"],
                            "p25": st["p25"], "p75": st["p75"]})
            if my_up < (st["p25"] or 0) - 0.02:
                imp = None
                if kind == "Дебафф" and me.dmg_by_ability.get(ab) and my_up > 0:
                    dot_dps = me.dmg_by_ability[ab] / (my_up * alive)
                    imp = (st["median"] - my_up) * alive * dot_dps / dur / my_dps
                add(Finding(
                    "Время действия", f"{kind} «{name(ab)}»: действует {my_up:.0%} времени против {st['median']:.0%} у эталона",
                    my=my_up, ref=st["median"], ref_p25=st["p25"], ref_p75=st["p75"], unit="доля",
                    impact=imp, severity=_sev(my_up, st), key=f"up:{ab}",
                    note="Доля времени, пока вы были живы. "
                         + ("Влияние оценено по урону этого эффекта." if imp else
                            "Влияние на DPS не выражено в уроне самого эффекта."),
                    focus=f"Держать «{name(ab)}» ≥ {st['p25']:.0%} (у вас {my_up:.0%})",
                    evidence=me.url))
    tables["uptime"] = up_rows

    # ---------------------------------------------- ресурсы и проки
    res_rows = []
    if mm["resource"] and agg["resource"]:
        for key, label in (("mean", "Средний уровень"), ("overcap", "Доля кастов у предела (≥95%)"),
                           ("starved", "Доля кастов на нуле (≤5%)"), ("before_burst", "Уровень перед бурстом")):
            st = agg["resource"][key]
            res_rows.append({"metric": label, "my": mm["resource"][key], "ref": st["median"],
                             "p25": st["p25"], "p75": st["p75"]})
        wst = stat_or_none([m["resource"]["waste"] for m in ref.metrics if m["resource"]])
        if wst and mm["resource"]["waste"] is not None:
            res_rows.append({"metric": "Потеряно сверх максимума (по событиям ресурса)",
                             "my": mm["resource"]["waste"], "ref": wst["median"], "p25": wst["p25"], "p75": wst["p75"]})
            if mm["resource"]["waste"] > (wst["p75"] or 0) + 0.02:
                add(Finding(
                    "Ресурсы", f"{mm['resource']['name']}: потеряно {mm['resource']['waste']:.0%} прироста "
                               f"против {wst['median']:.0%} у эталона",
                    my=mm["resource"]["waste"], ref=wst["median"], ref_p25=wst["p25"], ref_p75=wst["p75"],
                    unit="доля прироста", severity=_sev(mm["resource"]["waste"], wst), key="res:waste",
                    note="Точные потери из событий WCL: ресурс пришёл, когда шкала уже была полной.",
                    focus=f"Тратить {mm['resource']['name'].lower()} до достижения максимума",
                    evidence=me.url))
        elif mm["resource"]["overcap"] is not None:
            st = agg["resource"]["overcap"]
            if mm["resource"]["overcap"] > (st["p75"] or 0) + 0.02:
                add(Finding(
                    "Ресурсы", f"{mm['resource']['name']}: чаще у предела, чем у эталона",
                    my=mm["resource"]["overcap"], ref=st["median"], ref_p25=st["p25"], ref_p75=st["p75"],
                    unit="доля кастов", severity=_sev(mm["resource"]["overcap"], st), key="res:overcap",
                    note="Ресурс у максимума: новый прирост теряется.",
                    focus=f"Тратить {mm['resource']['name'].lower()} до достижения максимума",
                    evidence=me.url))
    tables["resource"] = res_rows

    proc_rows = []
    for ab, pr in ref.procs.items():
        mine = mm["procs"].get(ab, {"gained": 0, "used": 0, "expired": 0, "refreshed": 0, "reaction": None})
        a = agg["procs"][ab]
        lost_my = mine["expired"] + mine["refreshed"]
        lost_ref = (a["expired"]["median"] or 0) + (a["refreshed"]["median"] or 0)
        proc_rows.append({"id": ab, "name": pr.name, "consumer": pr.consumer_name, **mine,
                          "ref_gained": a["gained"]["median"], "ref_used": a["used"]["median"],
                          "ref_expired": a["expired"]["median"], "ref_refreshed": a["refreshed"]["median"],
                          "ref_reaction": a["reaction"]["median"]})
        if lost_my - lost_ref >= 2:
            gn = gain(pr.consumer)
            imp = (lost_my - lost_ref) * gn / dur / my_dps if gn else None
            add(Finding(
                "Проки", f"«{pr.name}»: потеряно {_n(lost_my, 'прок', 'прока', 'проков')} (у эталона {lost_ref:.0f})",
                my=lost_my, ref=lost_ref, unit="проков", impact=imp, severity=2 + (lost_my - lost_ref) / 2,
                key=f"proc:{ab}",
                note=f"Истекло {mine['expired']}, перезаписано {mine['refreshed']}. "
                     f"Тратится через {pr.consumer_name}.",
                focus=f"Тратить {pr.name} через {pr.consumer_name} до истечения",
                evidence=me.url))
    tables["procs"] = proc_rows

    # --------------------------------------------- приоритеты (п. 13)
    prio_rows = []
    for rule in agg.get("prio_rules", []):
        k, ab = rule["state"], rule["id"]
        n_state = mm["prio_state"].get(k, 0)
        st_name = _state_name(k, ref, mm)
        my_rate = mm["prio_next"].get((k, ab), 0) / n_state if n_state else None
        prio_rows.append({"state": st_name, "name": name(ab), "my": my_rate, "ref": rule["rate"]["median"],
                          "p25": rule["rate"]["p25"], "n": n_state})
        if n_state >= 5 and my_rate is not None and my_rate < (rule["rate"]["p25"] or 0) - 0.1:
            add(Finding(
                "Приоритет", f"{st_name}: топ жмёт «{name(ab)}» в {rule['rate']['median']:.0%} случаев, вы — {my_rate:.0%}",
                my=my_rate, ref=rule["rate"]["median"], ref_p25=rule["rate"]["p25"], unit="доля",
                severity=2 + 6 * ((rule["rate"]["median"] or 0) - my_rate), key=f"prio:{k[0]}:{k[1]}:{ab}",
                note=f"Правило выведено из логов топа (игроков: {rule['players']}): что они нажимают в этой ситуации. "
                     f"У вас такое состояние было {n_state} раз.",
                focus=f"{st_name} → «{name(ab)}»", evidence=me.url))
    tables["priority"] = prio_rows

    # ------------------------------------------------ опенер (п. 7)
    tables["opener"] = None
    os_ = agg.get("opener_seq")
    if os_ and len(mm["opener_seq"]) >= 5:
        cons, mine = os_["seq"], mm["opener_seq"][:len(os_["seq"])]
        sm = SequenceMatcher(None, cons, mine, autojunk=False)
        dist = 1 - sm.ratio()
        miss, extra = [], []
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op in ("delete", "replace"):
                miss += cons[i1:i2]
            if op in ("insert", "replace"):
                extra += mine[j1:j2]
        tables["opener"] = {"ref": [name(a) for a in cons], "my": [name(a) for a in mine], "dist": dist,
                            "ref_dist": os_["dist"]["median"]}
        limit = max((os_["dist"]["p75"] or 0) + 0.1, 0.3)
        if dist > limit and (miss or extra):
            add(Finding(
                "Опенер", "Опенер отличается от топа: "
                + (f"нет {', '.join(dict.fromkeys(name(a) for a in miss[:3]))}" if miss else "")
                + ("; " if miss and extra else "")
                + (f"лишнее {', '.join(dict.fromkeys(name(a) for a in extra[:3]))}" if extra else ""),
                my=dist, ref=os_["dist"]["median"], unit="расхождение", severity=2 + 5 * (dist - limit), key="opener",
                note="Сравнение последовательностей с выравниванием: пропуск или лишний каст не сдвигает всё остальное.",
                focus="Опенер: " + " → ".join(name(a) for a in cons[:8]), evidence=me.url))

    # ------------------------------------------------------- механики
    mech_rows, offsets = [], []
    for ab, mech in sorted(agg["mechanics"].items(), key=lambda kv: -kv[1]["damage"]):
        if not mech["is_key"] and ab not in mm["boss_timeline"]:
            continue
        my_ts = mm["boss_timeline"].get(ab) or [t for t, _, _ in _occurrences(mm["hits_taken"].get(ab, []))]
        for occ in mech["occurrences"][:8]:
            k = occ["k"] - 1
            my_t = my_ts[k] if k < len(my_ts) else None
            if my_t is not None and mech["is_key"]:
                offsets.append(abs(my_t - occ["median"]))
            mech_rows.append({"id": ab, "name": name(ab), "k": occ["k"], "ref": occ["median"],
                              "my": my_t, "key": mech["is_key"]})
    tables["mechanics"] = mech_rows

    ilvl_s = agg["ilvl"]
    tables["gear"] = {"my_ilvl": me.ilvl, "ref_ilvl": ilvl_s["median"], "my_trinkets": me.trinket_ids,
                      "ref_trinkets": _common_trinkets(ref_logs)}
    return findings, tables, offsets


def stat_or_none(vals):
    from .reference import stat
    vals = [v for v in vals if v is not None]
    return stat(vals) if len(vals) >= 3 else None


def _state_name(k: tuple, ref: Reference, mm: dict) -> str:
    kind, ab = k
    if kind == "buff":
        return f"Когда активен эффект «{ref.name_of(ab)}»"
    res = (mm.get("resource") or {}).get("name") or "ресурс"
    return f"{res}: ≥80%" if kind == "res_hi" else f"{res}: ≤20%"


def _hold_reasons(ref: Reference) -> dict[int, str]:
    """Под что топ придерживает кулдаун (п. 14): окно уязвимости, ключевая механика или начало фазы."""
    out = {}
    agg = ref.agg
    vuln = [(ab, w["median"]) for ab, d in agg.get("boss_debuffs", {}).items()
            if (d["uptime"]["median"] or 0) < 0.5 and d["self_share"] < 0.4 for w in d["windows"]]
    for ab, rows in agg["cd_timing"].items():
        if ref.spells[ab].category not in ("offensive", "racial", "trinket"):
            continue
        hits = defaultdict(int)
        for row in rows:
            t = row["median"]
            for vab, ws in vuln:
                if ws is not None and -3 <= t - ws <= 8:
                    hits[f"окно «{ref.name_of(vab)}»"] += 1
            if row.get("phase") is not None and row["phase_off"]["median"] is not None \
                    and row["phase_off"]["median"] <= 8 and row["k"] > 1:
                hits[f"начало фазы {row['phase']}"] += 1
        best = max(hits.items(), key=lambda kv: kv[1], default=None)
        if best and best[1] >= max(1, len(rows) // 2):
            out[ab] = best[0]
    return out


# ----------------------------------------------------------------- helpers
def _occurrences(hits: list[tuple[float, float, float | None]], gap: float = 3.0):
    """Схлопывает удары одной механики в «появления»: (время, суммарный урон, мин. HP)."""
    occ = []
    for t, a, hp in sorted(hits):
        if occ and t - occ[-1][0] <= gap:
            t0, s, h = occ[-1]
            occ[-1] = (t0, s + a, min([x for x in (h, hp) if x is not None], default=None))
        else:
            occ.append((t, a, hp))
    return occ


def _cd_uplift(me: PlayerLog, mm: dict, main_cd: int | None) -> dict | None:
    """Прирост урона одного окна главного CD по моему таймлайну урона."""
    if not main_cd or not mm.get("dps_5s") or main_cd not in mm["cast_times"]:
        return None
    windows = me.buffs.get(main_cd) or [(t, t + 20) for t in mm["cast_times"][main_cd]]
    win_len = float(np.mean([e - s for s, e in windows])) if windows else 20.0
    inside = outside = 0.0
    t_in = 0.0
    wins = merge_intervals(windows)
    for t, a in me.dmg_timeline:
        if any(s <= t <= e for s, e in wins):
            inside += a
        else:
            outside += a
    t_in = sum(e - s for s, e in wins)
    t_out = mm["alive"] - t_in
    if t_in <= 0 or t_out <= 0:
        return None
    gain = (inside / t_in - outside / t_out) * win_len
    return {"gain_per_use": max(0.0, gain), "window": win_len}


def _ref_defensive_share(ref: Reference, killer: int, t_death: float, mm: dict) -> float | None:
    my_occ = _occurrences(mm["hits_taken"].get(killer, []))
    k = next((i for i, (t, _, _) in enumerate(my_occ) if t <= t_death + 0.5 and t >= t_death - 6), None)
    if k is None:
        return None
    def_ids = {ab for ab, s in ref.spells.items() if s.category in ("defensive", "healthstone")}
    yes = tot = 0
    for m in ref.metrics:
        occ = _occurrences(m["hits_taken"].get(killer, []))
        if len(occ) <= k:
            continue
        t_hit = occ[k][0]
        tot += 1
        if any(t_hit - 8 <= t <= t_hit for ab in def_ids for t in m["cast_times"].get(ab, [])):
            yes += 1
    return yes / tot if tot else None


def _common_trinkets(logs: list[PlayerLog]) -> list[tuple[int, int]]:
    from collections import Counter
    c = Counter(t for log in logs for t in log.trinket_ids if t)
    return c.most_common(4)


def _gap(r: CompareResult, my_dps: float) -> list[dict]:
    total = (r.ref.agg["dps"]["median"] or 0) - r.me.dps
    cats = defaultdict(float)
    for f in r.findings:
        if f.impact and not f.noise and not f.context:
            cats[f.section] += f.impact * my_dps
    rows = [{"factor": k, "dps": v, "status": "Оценка по данным лога"} for k, v in
            sorted(cats.items(), key=lambda kv: -kv[1])]
    ref = r.ref
    xs = [(log.ilvl, log.dps) for log in ref.logs if log.ilvl]
    if r.me.ilvl and len(xs) >= 5 and np.std([x for x, _ in xs]) > 0.5:
        slope = float(np.polyfit([x for x, _ in xs], [y for _, y in xs], 1)[0])
        est = max(0.0, slope * ((ref.agg["ilvl"]["median"] or r.me.ilvl) - r.me.ilvl))
        if est > 0:
            rows.append({"factor": "Экипировка (уровень предметов)", "dps": est, "status": "Возможная причина"})
    for f in r.findings:
        if f.context:
            rows.append({"factor": f.title, "dps": 0.0, "status": "Вне вашего контроля, не оценено"})
    explained = sum(x["dps"] for x in rows)
    rows.append({"factor": "Не объяснено моделью", "dps": total - explained,
                 "status": "Остаток: другие факторы и пересечения"})
    return [{"total": total, **x} for x in rows]


def _reliability(me: PlayerLog, ref: Reference, mm: dict, mech_offsets: list[float]) -> dict:
    crit = []

    def add(name, level, reason):
        crit.append({"criterion": name, "level": level, "reason": reason})

    same = all(log.encounter_id == me.encounter_id and log.difficulty == me.difficulty
               and log.spec == me.spec for log in ref.logs)
    other_diff = sum(1 for log in ref.logs if log.difficulty != me.difficulty)
    add("Босс, сложность, спек", "HIGH" if same else "LOW",
        f"у всех {ref.n} логов топа — тот же босс, спек и сложность ({me.difficulty_name}), как в вашем бою" if same
        else (f"логов другой сложности: {other_diff} из {ref.n}" if other_diff else "есть логи другого босса или спека"))
    d = ref.agg["duration"]["median"] or me.duration
    rel = abs(me.duration - d) / d
    add("Длительность боя", "HIGH" if rel <= 0.1 else "MEDIUM" if rel <= 0.2 else "LOW",
        f"ваш бой {_fmt_t(me.duration)}, медиана эталона {_fmt_t(d)} ({rel:.0%})")
    add("Число эталонных логов", "HIGH" if ref.n >= 20 else "MEDIUM" if ref.n >= 10 else "LOW",
        f"{_n(ref.n, 'лог', 'лога', 'логов')}")
    if me.ilvl and ref.agg["ilvl"]["median"]:
        di = ref.agg["ilvl"]["median"] - me.ilvl
        add("Экипировка", "HIGH" if abs(di) <= 3 else "MEDIUM" if abs(di) <= 8 else "LOW",
            f"уровень предметов {me.ilvl:.0f} против {ref.agg['ilvl']['median']:.0f}")
    else:
        add("Экипировка", "—", "уровень предметов неизвестен, критерий не проверен")
    if mech_offsets:
        mo = float(np.median(mech_offsets))
        add("Тайминги механик (тактика)", "HIGH" if mo <= 5 else "MEDIUM" if mo <= 15 else "LOW",
            f"медианное отклонение ключевых механик {_c(mo)} с")
    else:
        add("Тайминги механик (тактика)", "—", "механики не сопоставлены")
    ref_lust = ref.agg["lust_share"] >= 0.5
    my_lust = mm["lust"] is not None
    add("Жажда крови (Героизм)", "HIGH" if ref_lust == my_lust else "MEDIUM",
        f"у вас {'есть' if my_lust else 'нет'}, у эталона {ref.agg['lust_share']:.0%}")
    dates = [log.report_start for log in ref.logs if log.report_start]
    if me.report_start and dates:
        days = abs(me.report_start - float(np.median(dates))) / 86400000
        add("Патч / дата", "HIGH" if days <= 21 else "MEDIUM" if days <= 60 else "LOW",
            f"разница дат отчётов {days:.0f} дн.")
    a_ref = ref.agg["adds_share"]["median"]
    if mm.get("adds_share") is not None and a_ref is not None:
        da = abs(mm["adds_share"] - a_ref)
        add("Профиль целей", "HIGH" if da <= 0.1 else "MEDIUM" if da <= 0.25 else "LOW",
            f"по аддам {mm['adds_share']:.0%} кастов, у эталона {a_ref:.0%}")
    else:
        add("Профиль целей", "—", "цели кастов неизвестны")
    if ref.build_note:
        add("Билд (таланты)", "HIGH" if "≥85%" in ref.build_note else "MEDIUM", ref.build_note[:1].lower() + ref.build_note[1:])
    miss_raid = [f for f in r_ctx(ref, mm) if f]
    add("Дебаффы рейда на боссе", "HIGH" if not miss_raid else "MEDIUM",
        "совпадают с топом" if not miss_raid else "не было: " + ", ".join(miss_raid))
    chk = ref.agg["dps_check"]["median"]
    if chk is not None and abs(chk - 1) > 0.05:
        add("Данные DPS", "MEDIUM", f"DPS рейтинга отличается от расчёта по таблице урона на {abs(chk - 1):.0%}")
    if me.dps_calc and me.dps and abs(me.dps / me.dps_calc - 1) > 0.05:
        add("Данные DPS", "MEDIUM", f"ваш DPS отличается от суммы таблицы урона на {abs(me.dps / me.dps_calc - 1):.0%}")
    levels = [c["level"] for c in crit if c["level"] in LEVELS]
    overall = min(levels, key=LEVELS.index) if levels else "LOW"
    return {"overall": overall, "criteria": crit}


def r_ctx(ref: Reference, mm: dict) -> list[str]:
    """Рейдовые дебаффы на боссе, которых не было в вашем бою."""
    out = []
    for ab, d in ref.agg.get("boss_debuffs", {}).items():
        up = d["uptime"]
        if d["self_share"] < 0.6 and (up["median"] or 0) >= 0.5 \
                and mm["uptime_boss"].get(ab, 0.0) < (up["p25"] or 0) - 0.15:
            out.append(f"«{ref.name_of(ab)}»")
    return out


def _summary(r: CompareResult) -> list[dict]:
    me, mm, agg = r.me, r.mm, r.ref.agg
    rows = [
        {"metric": "DPS", "my": me.dps, "ref": agg["dps"]["median"], "fmt": "num", "better": "higher"},
        {"metric": "Длительность боя, с", "my": me.duration, "ref": agg["duration"]["median"],
         "fmt": "num1", "better": None},
        {"metric": "Время живым", "my": mm["alive"] / me.duration, "ref": agg["alive"]["median"],
         "fmt": "pct", "better": "higher"},
        {"metric": "Касты за бой", "my": mm["total_casts"], "ref": agg["total_casts"]["median"],
         "fmt": "num", "better": "higher"},
    ]
    # Uptime ключевых эффектов: среднее по отслеживаемым дебаффам (иначе баффам)
    src_ref = agg["uptime_debuff"] or agg["uptime_buff"]
    src_my = mm["uptime_debuff"] if agg["uptime_debuff"] else mm["uptime_buff"]
    keys = [ab for ab, s in src_ref.items() if (s["median"] or 0) >= 0.15]
    if keys:
        rows.append({"metric": "Время действия ключевых эффектов", "fmt": "pct", "better": "higher",
                     "my": float(np.mean([src_my.get(ab, 0.0) for ab in keys])),
                     "ref": float(np.mean([src_ref[ab]["median"] for ab in keys]))})
    off = [ab for ab, s in r.ref.spells.items()
           if s.category in ("offensive", "racial", "trinket") and ab in agg["cd_uses"]]
    if off:
        def eff(m):
            u = [m["cd_usage"][ab] for ab in off if ab in m["cd_usage"]]
            return sum(x["used"] for x in u) / max(1, sum(x["ideal"] for x in u))
        rows.append({"metric": "Использование кулдаунов", "fmt": "pct", "better": "higher",
                     "my": eff(mm), "ref": med([eff(m) for m in r.ref.metrics])})
    if mm["resource"] and agg["resource"]:
        rows.append({"metric": "Ресурс без переполнения", "fmt": "pct", "better": "higher",
                     "my": 1 - mm["resource"]["overcap"], "ref": 1 - (agg["resource"]["overcap"]["median"] or 0)})
    rows.append({"metric": "Активное время (без простоя)", "fmt": "pct", "better": "higher",
                 "my": 1 - mm["gcd"]["idle_share"], "ref": 1 - (agg["gcd"]["idle_share"]["median"] or 0)})
    return rows


def _top5(findings: list[Finding], per_section: int = 2) -> list[Finding]:
    """Сначала оценённые по влиянию на DPS, затем Unknown по величине отклонения;
    не больше двух пунктов из одного раздела, чтобы план не состоял из одного навыка."""
    pool = [f for f in findings if not f.noise and not f.context]
    quant = sorted([f for f in pool if f.impact and f.impact >= 0.003], key=lambda f: -f.impact)
    unknown = sorted([f for f in pool if f.impact is None], key=lambda f: -f.severity)
    out, used = [], defaultdict(int)
    for f in quant + unknown:
        if used[f.section] < per_section:
            out.append(f)
            used[f.section] += 1
        if len(out) == 5:
            break
    return out


def _plan(top5: list[Finding]) -> list[dict]:
    plan = []
    for i, f in enumerate(top5[:3]):
        plan.append({"session": i + 1, "section": f.section, "focus": f.focus or f.title, "why": f.title,
                     "check": "После тренировки разберите новый лог и проверьте этот показатель."})
    if len(plan) >= 2:
        plan.append({"session": len(plan) + 1, "section": "Всё вместе", "focus": f"Всё из тренировок 1–{len(plan)} — в одном бою",
                     "why": "Закрепить навыки вместе",
                     "check": "Все показатели прошлых тренировок — на уровне эталона."})
    return plan


def _timeline(r: CompareResult) -> list[dict]:
    """События для двух дорожек: Top median и мой лог, плюс механики моего боя."""
    ev = []
    ref, mm, agg = r.ref, r.mm, r.ref.agg
    pot_name = next((s.name for s in ref.spells.values() if s.category == "potion"), "Зелье")
    for ab, rows in agg["cd_timing"].items():
        cat = ref.spells[ab].category
        if cat not in ("offensive", "defensive", "racial", "trinket"):
            continue
        for row in rows:
            ev.append({"lane": "Медиана топа", "t": row["median"], "cat": CATEGORY_RU[cat], "name": ref.spells[ab].name,
                       "k": row["k"]})
    for row in agg["potions"]:
        ev.append({"lane": "Медиана топа", "t": row["median"], "cat": "Зелье", "name": pot_name, "k": row["k"]})
    if agg["lust"]["median"] is not None and agg["lust_share"] >= 0.5:
        ev.append({"lane": "Медиана топа", "t": agg["lust"]["median"], "cat": "Жажда крови", "name": "Жажда крови", "k": 1})
    for ab, s in ref.spells.items():
        if s.category not in ("offensive", "defensive", "racial", "trinket"):
            continue
        for k, t in enumerate(mm["cast_times"].get(ab, [])):
            ev.append({"lane": "Мой лог", "t": t, "cat": CATEGORY_RU[s.category], "name": s.name, "k": k + 1})
    for k, t in enumerate(mm["potion_times"]):
        ev.append({"lane": "Мой лог", "t": t, "cat": "Зелье", "name": pot_name, "k": k + 1})
    if mm["lust"] is not None:
        ev.append({"lane": "Мой лог", "t": mm["lust"], "cat": "Жажда крови", "name": "Жажда крови", "k": 1})
    # На таймлайн — до трёх ключевых механик с наибольшим уроном за одно появление
    key = sorted([ab for ab, m in agg["mechanics"].items() if m["is_key"] and len(m["occurrences"]) <= 20],
                 key=lambda ab: -agg["mechanics"][ab]["damage"] / len(agg["mechanics"][ab]["occurrences"]))[:3]
    for ab in key:
        occ = _occurrences(mm["hits_taken"].get(ab, []))
        for k, (t, _, _) in enumerate(occ):
            ev.append({"lane": "Механики", "t": t, "cat": "Механика", "name": ref.name_of(ab), "k": k + 1})
    # Отметка расхождения: моё k-е применение дальше 10 с от эталонного
    ref_t = {(e["name"], e["k"]): e["t"] for e in ev if e["lane"] == "Медиана топа"}
    for e in ev:
        if e["lane"] == "Мой лог" and (e["name"], e["k"]) in ref_t:
            e["delta"] = e["t"] - ref_t[(e["name"], e["k"])]
    return sorted(ev, key=lambda e: (e["lane"], e["t"]))


def _n(n: float, one: str, few: str, many: str, cnt: bool = False) -> str:
    """Число с существительным в правильной форме: 1 лог, 3 лога, 21 лог, 25 логов."""
    k = int(round(n))
    a, b = k % 10, k % 100
    w = one if a == 1 and b != 11 else few if 2 <= a <= 4 and not 12 <= b <= 14 else many
    return f"{k} {w}"


def _sec(off: float) -> str:
    """Смещение относительно главного кулдауна: «за 2 с до» / «через 1 с» / «одновременно»."""
    v = round(off)
    return "одновременно" if v == 0 else f"за {-v} с до" if v < 0 else f"через {v} с"


def _c(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def _pct(x: float) -> str:
    return f"{x * 100:.1f}".replace(".", ",") + "%"


def _brief(r: CompareResult) -> dict:
    """Выжимка: вердикт, 3 действия, что уже хорошо, контекст. Остальное — в подробностях."""
    me, ref, agg = r.me, r.ref, r.ref.agg
    ref_dps = agg["dps"]["median"] or 0
    gap = (me.dps - ref_dps) / ref_dps if ref_dps else 0.0
    num = lambda x: f"{x:,.0f}".replace(",", " ")  # noqa: E731
    verdict = (f"{num(me.dps)} DPS — " + (f"на {_pct(-gap)} ниже" if gap < 0 else f"на {_pct(gap)} выше")
               + f" медианы {ref.label} ({num(ref_dps)})")
    actions = []
    for f in r.top5[:3]:
        actions.append({"title": f.title, "do": f.focus or f.title, "section": f.section, "level": f.impact_level,
                        "gain": (f"≈ +{_pct(f.impact)} DPS" if f.impact else "влияние на DPS не оценено"),
                        "why": f.note.split(". ")[0].rstrip(".") + "." if f.note else ""})
    good = []
    for x in r.summary:
        if x.get("better") == "higher" and x["my"] is not None and x["ref"] is not None \
                and x["metric"] not in ("DPS", "Касты за бой", "Время живым") and x["my"] >= x["ref"]:
            good.append(f"{x['metric']}: {_pct(x['my'])} (топ {_pct(x['ref'])})" if x["fmt"] == "pct"
                        else f"{x['metric']}: {x['my']:.0f} (топ {x['ref']:.0f})")
    context = [f.title for f in r.findings if f.context][:3]
    rel = r.reliability
    weak = [c["criterion"] for c in rel["criteria"] if c["level"] in ("LOW", "MEDIUM")]
    quant = sum(f.impact for f in r.findings if f.impact and not f.noise and not f.context)
    noise_n = sum(1 for f in r.findings if f.noise)
    return {"verdict": verdict, "gap": gap, "reliability": rel["overall"],
            "reliability_note": ("слабые места: " + ", ".join(weak[:3])) if weak else "все критерии в норме",
            "actions": actions, "good": good[:2], "context": context,
            "explained": quant, "noise_hidden": noise_n,
            "deaths": len(r.mm["deaths"])}

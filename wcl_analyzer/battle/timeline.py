"""Лента боя: сырые факты → BattleEvent → объединение близких событий → значимость → 10–20 карточек.

Факты берутся из уже посчитанного сравнения с топом (compare.py) и из движения (movement.py):
модуль ничего не пересчитывает заново и не скачивает.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median

from .model import MUST, BattleEvent, Line
from .movement import burst_movement

MERGE_S = 3.0            # события ближе этого — одна карточка
MAX_LINES = 4
KIND_ICON = {"Тринкет": "🔮", "Зелье": "💊", "Расовая": "🟠", "Жажда крови": "🥁", "Способность": "🟣"}
REASON_RU = {"mechanic": "механика", "target": "к цели", "unknown": "причина неизвестна"}
SEV_ORDER = {None: 0, "low": 1, "medium": 2, "high": 3}


def mmss(t: float | None) -> str:
    if t is None:
        return "—"
    t = max(0.0, t)
    return f"{int(t // 60)}:{int(t % 60):02d}"


def sec(v: float, signed: bool = True) -> str:
    s = f"{v:+.1f}" if signed else f"{v:.1f}"
    return s.replace(".", ",").replace("-", "−") + " с"


def target_count(duration: float) -> int:
    """10–20 карточек: примерно одна на 20 секунд боя."""
    return max(10, min(20, round(duration / 20)))


def build(r, mv: dict) -> tuple[list[BattleEvent], list[BattleEvent]]:
    """(выбранные карточки, все карточки) — обе по времени."""
    me, mm, T, ref = r.me, r.mm, r.tables, r.ref
    name = ref.name_of
    ev: list[BattleEvent] = []
    findings = {f.key: f for f in r.findings if f.key and not f.noise}

    # ---------- опенер
    op = T.get("opener") or {}
    if op.get("my"):
        e = BattleEvent(0.0, "opener", "Опенер", window=(0.0, 12.0))
        e.lines.append(Line("▶", " → ".join(op["my"][:4])))
        if op.get("ref"):
            e.lines.append(Line("⭐", "Топ: " + " → ".join(op["ref"][:4]), "top"))
        f = findings.get("opener")
        e.severity = "medium" if f else None
        ev.append(e)

    # ---------- фазы и конец боя
    for t, pid in me.phases:
        if t > 1.0:
            ev.append(BattleEvent(t, "phase", f"Фаза {pid}", phase=pid))
    ev.append(BattleEvent(me.duration, "end", "Босс убит" if me.kill else "Вайп",
                          lines=[Line("⏱", f"Длительность {mmss(me.duration)}")]))

    # ---------- механики босса (ключевые, по урону)
    # ключевые механики с отдельными «ударами» (не фоновый урон каждые несколько секунд),
    # по урону за одно применение
    mech_dmg = {ab: m["damage"] / max(1, len(m.get("occurrences") or [])) for ab, m in ref.agg.get("mechanics", {}).items()
                if m.get("is_key") and 0 < len(m.get("occurrences") or []) <= max(3, me.duration / 20)}
    top_mech = sorted(mech_dmg, key=lambda a: -mech_dmg[a])[:3]
    for row in T.get("mechanics") or []:
        if not row["key"] or row["id"] not in top_mech:
            continue
        t = row["my"] if row["my"] is not None else row["ref"]
        if t is None or t > me.duration:
            continue
        hits = [(ht, amt, hp) for ht, ab, amt, hp in me.dmg_taken if ab == row["id"] and t - 1 <= ht <= t + 6]
        e = BattleEvent(t, "mechanic", f"«{row['name']}»", mechanic=row["name"], window=(t - 3, t + 8))
        if hits:
            dmg = sum(h[1] for h in hits)
            hp = hits[-1][2]
            e.lines.append(Line("🔴", f"Урон по вам {_big(dmg)}" + (f", здоровье после — {hp:.0f}%" if hp is not None else ""),
                                "bad" if hp is not None and hp < 35 else ""))
        else:
            e.lines.append(Line("🔴", "По вам не попало"))
        e.score = 10 * (3 - top_mech.index(row["id"]))
        ev.append(e)

    # ---------- защитные под удары
    for d in T.get("defensives") or []:
        t = d["t_hit"]
        e = BattleEvent(t, "defensive", d["name"], ability=d["name"], mechanic=d["mechanic"], window=(t - 4, t + 4))
        if d["my_lead"] is None:
            e.lines.append(Line("🛡", f"{d['name']}: не нажата", "bad"))
            e.severity = "high" if d["ref_share"] >= 0.5 else "low"
        else:
            when = f"за {sec(d['my_lead'], False)} до удара" if d["my_lead"] >= 0 else f"через {sec(-d['my_lead'], False)} после удара"
            e.lines.append(Line("🛡", f"{d['name']}: {when}"))
            diff = (d["ref_lead"] or 0) - d["my_lead"]  # плюс — позже топа
            e.diff = {"lead_s": round(-diff, 1)}
            if abs(diff) >= 2:
                e.severity = "medium" if abs(diff) < 4 else "high"
        if d["ref_lead"] is not None:
            e.lines.append(Line("⭐", f"Топ: за {sec(d['ref_lead'], False)} до удара, жмут {d['ref_share']:.0%}", "top"))
            e.top = {"lead_s": round(d["ref_lead"], 1), "share": round(d["ref_share"], 2)}
        e.lines.insert(0, Line("🔴", f"«{d['mechanic']}» №{d['k']}"))
        ev.append(e)

    # ---------- окна бурста
    rows_by_w = defaultdict(list)
    for x in T.get("burst") or []:
        rows_by_w[x["window"]].append(x)
    ref_pairs = list(zip(ref.logs, ref.metrics))
    for w, rows in sorted(rows_by_w.items()):
        head = rows[0]
        my_t0, ref_t0 = head["my_t0"], head["ref_t0"]
        if my_t0 is None:
            e = BattleEvent(ref_t0, "burst", f"Бурст №{w}: не нажат", ability=head["name"], severity="high",
                            window=(ref_t0, ref_t0 + 20))
            e.lines.append(Line("⭐", f"Топ: «{head['name']}» в {mmss(ref_t0)}", "top"))
            ev.append(e)
            continue
        e = BattleEvent(my_t0, "burst", f"Бурст №{w}", end=my_t0 + 20, ability=head["name"], window=(my_t0 - 2, my_t0 + 20))
        comps = []
        for x in rows:
            icon = KIND_ICON.get(x.get("kind") or "Способность", "🟣")
            if x["my_offset"] is None:
                comps.append(f"{icon} {x['name']} — нет")
            elif x is head:
                comps.append(f"{icon} {x['name']}")
            else:
                comps.append(f"{icon} {x['name']} {sec(x['my_offset'])}")
        e.lines.append(Line("💥", f"Бурст №{w}: " + " · ".join(comps)))
        d0 = my_t0 - ref_t0
        e.top, e.diff = {"t0": round(ref_t0, 1)}, {"t0_s": round(d0, 1)}
        e.lines.append(Line("⭐", f"Топ начинает в {mmss(ref_t0)} · у вас {sec(d0)}", "bad" if abs(d0) > 10 else "top"))
        my_mv, top_mv = burst_movement(mv, my_t0, ref_pairs, w - 1)
        if my_mv is not None and (my_mv >= 0.5 or (top_mv or 0) >= 0.5):
            txt = f"Движение в бурсте: {sec(my_mv, False)}" + (f" · топ {sec(top_mv, False)}" if top_mv is not None else "")
            e.lines.append(Line("📍", txt, "bad" if top_mv is not None and my_mv - top_mv >= 2 else ""))
            e.diff["move_s"] = round(my_mv - (top_mv or 0), 1) if top_mv is not None else None
        missing = sum(1 for x in rows[1:] if x["my_offset"] is None or abs(x["my_offset"] - x["ref_offset"]) > 3)
        if abs(d0) > 10 or missing >= 3:
            e.severity = "high"
        elif missing or abs(d0) > 5:
            e.severity = "medium"
        ev.append(e)

    # ---------- кулдауны вне бурста, нажатые сильно не вовремя
    in_burst = {x["id"] for x in T.get("burst") or []}
    for c in T.get("cd_timing") or []:
        if c["id"] in in_burst or c["my"] is None or c["ref"] is None or (c.get("confidence") or 0) < 0.5:
            continue
        dd = c["my"] - c["ref"]
        if abs(dd) < 8:
            continue
        # id −1 — зелья (все виды зелья сведены в одну строку)
        kind = "potion" if c["id"] == -1 or (c["id"] in ref.spells and ref.spells[c["id"]].category == "potion") else "cooldown"
        e = BattleEvent(c["my"], kind, f"«{c['name']}» №{c['k']}", ability=c["name"],
                        severity="medium" if abs(dd) < 20 else "high", window=(c["my"] - 3, c["my"] + 5))
        e.lines.append(Line("💊" if kind == "potion" else "🟣", f"«{c['name']}» №{c['k']}: топ жмёт в {mmss(c['ref'])} · у вас {sec(dd)}", "bad"))
        e.top, e.diff = {"t": round(c["ref"], 1)}, {"t_s": round(dd, 1)}
        ev.append(e)

    # ---------- движение
    for m in mv.get("moves") or []:
        e = BattleEvent(m["start"], "movement", "Движение", end=m["end"], movement=m["i"],
                        mechanic=m["mechanic"], window=(m["start"] - 2, m["end"] + 2))
        why = REASON_RU[m["reason"]] + (f" «{m['mechanic']}»" if m["mechanic"] else "")
        e.lines.append(Line("📍", f"{sec(m['moving'], False)}, {m['dist']:.0f} ярд. · {why}".replace(".0 ", " ")))
        if m["top_window"] is not None:
            e.top, e.diff = {"moving_s": m["top_window"], "share": m["top_share"]}, {"moving_s": m["diff"]}
            txt = f"Топ здесь: {sec(m['top_window'], False)}, двигаются {m['top_share']:.0%}"
            e.lines.append(Line("⭐", txt, "bad" if (m["diff"] or 0) >= 2 else "top"))
            if (m["diff"] or 0) >= 4:
                e.severity = "high"
            elif (m["diff"] or 0) >= 2 or (m["top_share"] is not None and m["top_share"] < 0.3 and m["moving"] >= 1):
                e.severity = "medium"  # топ в это время стоит на месте
            if m["top_share"] is not None and m["top_share"] < 0.3:
                e.lines[-1] = Line("⭐", f"Топ здесь стоит: двигаются {m['top_share']:.0%}", "bad")
        e.score = min(15.0, m["moving"] * 3)
        ev.append(e)

    # ---------- смена цели
    ev += _switches(me, ref)

    # ---------- смерти
    deaths = {round(d["t"], 1): d for d in T.get("deaths") or [] if isinstance(d.get("t"), (int, float))}
    for t in me.deaths:
        d = deaths.get(round(t, 1)) or {}
        e = BattleEvent(t, "death", "Смерть", severity="high", window=(t - 8, t + 1))
        if d.get("sources"):
            e.lines.append(Line("🔴", ", ".join(f"«{n}»" for n, _ in d["sources"][:2])))
        hp = [h for ht, _, _, h in me.dmg_taken if t - 6 <= ht <= t and h is not None]
        if hp:
            e.lines.append(Line("❤", f"Здоровье: {max(hp):.0f}% → 0%"))
        defs = d.get("defensives") or []
        e.lines.append(Line("🛡", ("Защита: " + ", ".join(defs)) if defs else "Защита: не нажата", "" if defs else "bad"))
        if d.get("ref_share") is not None:
            e.lines.append(Line("⭐", f"Топ в этот момент жмёт защиту: {d['ref_share']:.0%}", "top"))
        ev.append(e)

    # ---------- длинный простой
    for g in T.get("gcd_worst") or []:
        if g["idle"] < 2.5 or g["idle"] < 1.5 * (g.get("ref_bucket_idle") or 0):
            continue
        e = BattleEvent(g["from"], "idle", f"Простой {sec(g['idle'], False)}", end=g["to"], window=(g["from"] - 1, g["to"] + 1),
                        severity="medium" if g["idle"] < 4 else "high")
        e.lines.append(Line("⏸", f"Простой {sec(g['idle'], False)}: {g['prev_name']} → {g['next_name']}"))
        ev.append(e)

    # ---------- фаза каждого события
    ph = sorted(me.phases)
    for e in ev:
        if e.phase is None and ph:
            e.phase = max((pid for t, pid in ph if t <= e.t + 0.01), default=ph[0][1])
    cards = _merge(sorted(ev, key=lambda x: (x.t, -_prio(x))))
    for c in cards:
        c.finalize_score()
    picked = _select(cards, target_count(me.duration))
    return picked, cards


def _prio(e: BattleEvent) -> int:
    return 1000 if e.type in MUST else int(e.score) + {"death": 90, "burst": 70, "defensive": 55}.get(e.type, 40)


def _merge(evs: list[BattleEvent]) -> list[BattleEvent]:
    """Близкие по времени события — в одну карточку. Фазы и конец боя не сливаются ни с чем."""
    from .model import BASE_SCORE
    cards: list[BattleEvent] = []
    for e in evs:
        c = cards[-1] if cards else None
        mergeable = (c is not None and e.t - c.t <= MERGE_S and c.type not in ("phase", "end")
                     and e.type not in ("phase", "end", "opener") and not (c.type == "burst" and e.type == "burst"))
        if not mergeable:
            e.merged = [e.type]
            cards.append(e)
            continue
        # главная — более значимая по типу; её строки первыми
        if BASE_SCORE.get(e.type, 0) > BASE_SCORE.get(c.type, 0) and c.type != "opener":
            main, other = e, c
            cards[-1] = main
            main.merged = [main.type] + c.merged
            main.t = min(main.t, other.t)
        else:
            main, other = c, e
            main.merged.append(e.type)
        main.lines = (main.lines + other.lines)[:MAX_LINES]
        main.severity = max(main.severity, other.severity, key=lambda s: SEV_ORDER[s])
        main.mechanic = main.mechanic or other.mechanic
        main.movement = main.movement if main.movement is not None else other.movement
        if other.window and main.window:
            main.window = (min(main.window[0], other.window[0]), max(main.window[1], other.window[1]))
        main.window = main.window or other.window
        main.score = max(main.score, other.score) + 5
        main.top = {**other.top, **main.top}
        main.diff = {**other.diff, **main.diff}
    return cards


def _select(cards: list[BattleEvent], n: int) -> list[BattleEvent]:
    """Обязательные карточки + самые значимые. Повторяющаяся механика без отклонений и без
    действий игрока — не больше двух раз: третий одинаковый «удар босса» ничего не добавляет."""
    must = [c for c in cards if c.type in MUST]
    rest = sorted((c for c in cards if c.type not in MUST), key=lambda c: -c.score)
    room = max(0, n - len(must))
    seen: dict = {}
    picked = []
    for c in rest:
        if len(picked) >= room:
            break
        plain = c.type == "mechanic" and len(c.merged) == 1 and not c.severity
        if plain:
            seen[c.title] = seen.get(c.title, 0) + 1
            if seen[c.title] > 2:
                continue
        picked.append(c)
    return sorted(must + picked, key=lambda c: c.t)


def _switches(me, ref) -> list[BattleEvent]:
    """Значимые переключения: с босса на другую цель, на которой игрок провёл хотя бы 3 каста.
    Сравнение с топом — по времени первого каста топа в цель с тем же именем."""
    bosses = me.boss_ids
    out = []
    seq = sorted(me.target_seq)
    i = 0
    while i < len(seq):
        t, tg = seq[i]
        prev = seq[i - 1][1] if i else None
        if tg not in bosses and prev in bosses:
            j = i
            while j < len(seq) and seq[j][1] == tg:
                j += 1
            if j - i >= 3:
                tname = me.actor_names.get(tg, "цель")
                tops = [_first_on(log, tname, t) for log in ref.logs]
                tops = [x for x in tops if x is not None]
                e = BattleEvent(t, "switch", f"Смена цели → «{tname}»", end=seq[j - 1][0], window=(t - 3, t + 5))
                e.lines.append(Line("🎯", f"Босс → «{tname}», {j - i} кастов"))
                if len(tops) >= 3:
                    tm = median(tops)
                    dd = t - tm
                    e.top, e.diff = {"t": round(tm, 1)}, {"t_s": round(dd, 1)}
                    e.lines.append(Line("⭐", f"Топ переключается в {mmss(tm)} · у вас {sec(dd)}", "bad" if dd >= 1.5 else "top"))
                    e.severity = "high" if dd >= 4 else "medium" if dd >= 1.5 else None
                out.append(e)
            i = j
        else:
            i += 1
    return out


def _first_on(log, tname: str, near: float) -> float | None:
    ids = {aid for aid, n in log.actor_names.items() if n == tname}
    for t, tg in sorted(log.target_seq):
        if tg in ids and near - 20 <= t <= near + 20:
            return t
    return None


def _big(v: float) -> str:
    if v >= 1e6:
        return f"{v / 1e6:.1f} млн".replace(".", ",")
    if v >= 1e3:
        return f"{v / 1e3:.0f} тыс."
    return f"{v:.0f}"

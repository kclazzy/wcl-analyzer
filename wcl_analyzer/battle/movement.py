"""Движение игрока и сравнение с топом.

Причина движения указывается, только если её подтверждают данные:
  «механика» — за ≤ 4 с до начала движения босс применил способность И большинство топа
               в это же время тоже двигается;
  «к цели»   — во время движения игрок сменил цель;
  иначе      — «неизвестно». Зоны на полу (AoE) в логах не описаны, «уклонение» не угадываем.
"""
from __future__ import annotations

from statistics import median

from . import positions as P

MECH_BEFORE_S = 4.0
WINDOW_PAD_S = 2.0
TOP_MOVING_SHARE = 0.5   # доля топа, которая тоже двигается, — движение «общее» для боя


class Track:
    """Движение одного игрока в бою: точки относительно босса, упрощённые точки, участки."""

    def __init__(self, log):
        self.log = log
        rel, self.origin = P.relative(log.positions, log.boss_positions)
        self.points = P.denoise(rel)
        self.segs = P.segments(self.points)

    @property
    def ok(self) -> bool:
        return len(self.points) >= 5

    def total(self) -> float:
        return sum(s["moving"] for s in self.segs)

    def dist(self) -> float:
        return sum(s["dist"] for s in self.segs)

    def moving(self, a: float, b: float) -> float:
        return P.moving_time(self.segs, a, b)


def _pts(points, digits: int = 1) -> list[list[float]]:
    return [[round(t, 1), round(x, digits), round(y, digits)] for t, x, y in points]


def analyze(me, ref_logs, boss_name_of) -> dict:
    """Движения игрока, сравнение с медианой топа и данные карты."""
    mine = Track(me)
    tops = [Track(log) for log in ref_logs]
    tops = [t for t in tops if t.ok]
    out: dict = {"available": mine.ok, "origin": mine.origin, "top_n": len(tops)}
    if not mine.ok:
        return out

    my_targets = sorted(me.target_seq)
    moves = []
    for i, s in enumerate(mine.segs):
        a, b = s["start"], s["end"]
        wa, wb = a - WINDOW_PAD_S, b + WINDOW_PAD_S
        my_mv = mine.moving(wa, wb)
        top_mv = [t.moving(wa, wb) for t in tops]
        share = (sum(1 for v in top_mv if v >= 0.5) / len(top_mv)) if top_mv else None
        top_med = median(top_mv) if top_mv else None
        mech = [(bt, ab) for bt, ab in me.boss_casts if a - MECH_BEFORE_S <= bt <= a + 0.5]
        mech_name = boss_name_of(mech[-1][1]) if mech else ""
        switched = _switch_inside(my_targets, a, b)
        if switched:
            reason = "target"
        elif mech and share is not None and share >= TOP_MOVING_SHARE:
            reason = "mechanic"
        else:
            reason = "unknown"
        rep = _representative(tops, wa, wb, top_med)
        moves.append({
            "i": i, "start": round(a, 1), "end": round(b, 1), "moving": round(s["moving"], 1),
            "dist": round(s["dist"], 1), "net": round(s["net"], 1), "reason": reason,
            "mechanic": mech_name if reason == "mechanic" else "",
            "near_mechanic": mech_name,  # что применял босс рядом по времени (без вывода о причине)
            "my_window": round(my_mv, 1), "top_window": round(top_med, 1) if top_med is not None else None,
            "top_share": round(share, 2) if share is not None else None,
            "diff": round(my_mv - top_med, 1) if top_med is not None else None,
            "top_log": _log_ref(rep.log) if rep else None,
        })

    tot_top = [t.total() for t in tops]
    out.update({
        "moves": moves,
        "total": round(mine.total(), 1), "dist": round(mine.dist(), 1),
        "top_total": round(median(tot_top), 1) if tot_top else None,
        "top_dist": round(median([t.dist() for t in tops]), 1) if tops else None,
        "diff": round(mine.total() - median(tot_top), 1) if tot_top else None,
        # вся траектория — для карты, повтора и тепловой карты. Без пространственного упрощения:
        # оно выбрасывает точки «стоял на месте» и ломает время. Шум уже убран, точек немного.
        "path": _pts(mine.points),
        "top_path": _pts(_rep_full(tops, tot_top).points) if tops else [],
        "top_log": _log_ref(_rep_full(tops, tot_top).log) if tops else None,
        "bounds": _bounds(mine.points),
    })
    out["_tracks"] = (mine, tops)  # для расчёта движения в окнах бурста (в JSON не попадает)
    return out


def burst_movement(mv: dict, my_t0: float | None, ref_metrics: list[dict], window: int, length: float = 20.0):
    """Движение в окне бурста: у вас и медиана топа в его собственном окне того же номера."""
    if not mv.get("_tracks") or my_t0 is None:
        return None, None
    mine, tops = mv["_tracks"]
    my = mine.moving(my_t0, my_t0 + length)
    by_log = {id(t.log): t for t in tops}
    vals = []
    for log, m in ref_metrics:
        t = by_log.get(id(log))
        wins = m.get("burst") or []
        if t and len(wins) > window:
            t0 = wins[window]["t0"]
            vals.append(t.moving(t0, t0 + length))
    return my, (median(vals) if vals else None)


def _switch_inside(targets, a: float, b: float) -> bool:
    before = [tg for t, tg in targets if t < a]
    inside = [tg for t, tg in targets if a <= t <= b + 1.0]
    return bool(before and inside and inside[-1] != before[-1])


def _log_ref(log) -> dict:
    return {"name": log.name, "url": getattr(log, "url", "") or ""}


def _representative(tops, a, b, med):
    """Лог топа, у которого движение в окне ближе всего к медиане (для наложения на карте)."""
    if not tops or med is None:
        return None
    return min(tops, key=lambda t: abs(t.moving(a, b) - med))


def _rep_full(tops, totals):
    med = median(totals)
    return min(tops, key=lambda t: abs(t.total() - med))


def _bounds(points) -> float:
    """Радиус карты: дальше всех от босса + запас, не меньше 20 ярдов."""
    r = max((max(abs(x), abs(y)) for _, x, y in points), default=0.0)
    return round(max(20.0, r * 1.15 + 3.0), 1)

"""Координаты: относительно босса, без шума, упрощённая траектория, участки движения.

Координаты берутся из событий, которые уже загружены для разбора (касты и полученный урон
с ресурсами), поэтому лишних запросов к WCL нет. Точек примерно одна в секунду: точное время
каждого шага неизвестно, длительность движения оценивается по расстоянию и скорости бега.
"""
from __future__ import annotations

import math
from bisect import bisect_left
from statistics import median

RUN_SPEED = 7.0        # ярдов в секунду — обычная скорость бега
NOISE_YD = 1.5         # смещение меньше этого между точками — шум/шаг на месте
MERGE_GAP_S = 1.5      # движения с паузой меньше этого — одно движение
MIN_MOVE_YD = 6.0      # значимое движение — от стольких ярдов
MAX_SAMPLE_GAP_S = 6.0  # между точками дольше — не знаем, что было: движение не засчитываем


def boss_center(boss: list[tuple[float, float, float]]):
    """Функция t → (x, y) босса: ближайшая точка по времени; без точек — None."""
    if not boss:
        return None
    ts = [b[0] for b in boss]

    def at(t: float) -> tuple[float, float]:
        i = bisect_left(ts, t)
        cand = [boss[j] for j in (i - 1, i) if 0 <= j < len(boss)]
        b = min(cand, key=lambda p: abs(p[0] - t))
        return b[1], b[2]
    return at


def relative(points, boss) -> tuple[list[tuple[float, float, float]], str]:
    """Точки игрока относительно босса (босс в начале координат). Если координат босса нет —
    относительно среднего положения игрока. Ось y направлена «вверх» карты."""
    if not points:
        return [], "none"
    at = boss_center(boss)
    if at is None:
        cx, cy = median(p[1] for p in points), median(p[2] for p in points)
        return [(t, x - cx, y - cy) for t, x, y in points], "player"
    out = []
    for t, x, y in points:
        bx, by = at(t)
        out.append((t, x - bx, y - by))
    return out, "boss"


def denoise(points):
    """Убираем дрожание координат: точка остаётся, только если ушла от предыдущей больше чем на шум."""
    if not points:
        return []
    out = [points[0]]
    prev = points[0]
    for p in points[1:]:
        if math.dist(p[1:], out[-1][1:]) >= NOISE_YD or p[0] - out[-1][0] > MAX_SAMPLE_GAP_S:
            # последняя точка «на месте» перед уходом: от неё и считается начало движения
            if prev is not out[-1] and math.dist(prev[1:], out[-1][1:]) < NOISE_YD:
                out.append((prev[0], out[-1][1], out[-1][2]))
            out.append(p)
        prev = p
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


def rdp(points, eps: float):
    """Рамер — Дуглас — Пекер: упрощение траектории, время сохраняется у оставшихся точек."""
    if len(points) < 3:
        return list(points)
    (_, x1, y1), (_, x2, y2) = points[0], points[-1]
    dx, dy = x2 - x1, y2 - y1
    norm = math.hypot(dx, dy)
    best, idx = -1.0, 0
    for i in range(1, len(points) - 1):
        _, x, y = points[i]
        d = (abs(dy * x - dx * y + x2 * y1 - y2 * x1) / norm) if norm > 1e-9 else math.hypot(x - x1, y - y1)
        if d > best:
            best, idx = d, i
    if best <= eps:
        return [points[0], points[-1]]
    return rdp(points[:idx + 1], eps)[:-1] + rdp(points[idx:], eps)


def segments(points) -> list[dict]:
    """Участки движения: склеиваем шаги с паузой < MERGE_GAP_S, оставляем от MIN_MOVE_YD ярдов.
    Длительность шага — расстояние / скорость бега, но не больше промежутка между точками."""
    steps = []
    for a, b in zip(points, points[1:]):
        dt = b[0] - a[0]
        d = math.dist(a[1:], b[1:])
        if d < NOISE_YD or dt <= 0 or dt > MAX_SAMPLE_GAP_S:
            continue
        dur = min(dt, max(d / RUN_SPEED, 0.3))
        steps.append((b[0] - dur, b[0], d, a, b))
    segs: list[dict] = []
    for st, en, d, a, b in steps:
        if segs and st - segs[-1]["end"] <= MERGE_GAP_S:
            s = segs[-1]
            s["end"], s["dist"], s["moving"] = en, s["dist"] + d, s["moving"] + (en - st)
            s["to"] = b
        else:
            segs.append({"start": st, "end": en, "dist": d, "moving": en - st, "from": a, "to": b})
    out = []
    for s in segs:
        if s["dist"] < MIN_MOVE_YD:
            continue
        out.append({"start": s["start"], "end": s["end"], "dist": s["dist"], "moving": s["moving"],
                    "from": s["from"], "to": s["to"],
                    "net": math.dist(s["from"][1:], s["to"][1:])})
    return out


def moving_time(segs: list[dict], a: float, b: float) -> float:
    """Сколько секунд из окна [a, b] игрок был в движении (по значимым движениям)."""
    tot = 0.0
    for s in segs:
        lo, hi = max(a, s["start"]), min(b, s["end"])
        if hi > lo:
            # «moving» внутри сегмента распределено равномерно по его длительности
            span = max(s["end"] - s["start"], 1e-6)
            tot += (hi - lo) * s["moving"] / span
    return tot


def window_points(points, a: float, b: float):
    """Точки траектории в окне [a, b] плюс по одной соседней с краёв — чтобы линия не обрывалась."""
    if not points:
        return []
    ts = [p[0] for p in points]
    i = max(0, bisect_left(ts, a) - 1)
    j = min(len(points), bisect_left(ts, b) + 1)
    return points[i:j]

"""Простая статистика без numpy: среднее, медиана, перцентиль (как numpy по умолчанию — линейная
интерполяция), стандартное отклонение (генеральное, как np.std), разности и наклон прямой.
numpy был нужен только для этого, а весит десятки мегабайт в .exe и APK и замедлял запуск."""
from __future__ import annotations

import math


def mean(vals) -> float:
    vals = list(vals)
    if not vals:
        return math.nan
    return float(sum(vals)) / len(vals)


def median(vals) -> float:
    return percentile(vals, 50)


def percentile(vals, q: float) -> float:
    """Как numpy.percentile(vals, q) по умолчанию (method="linear")."""
    xs = sorted(float(v) for v in vals)
    if not xs:
        return math.nan
    pos = (len(xs) - 1) * float(q) / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def std(vals) -> float:
    vals = list(vals)
    if not vals:
        return math.nan
    m = mean(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))


def diff(vals) -> list[float]:
    vals = list(vals)
    return [b - a for a, b in zip(vals, vals[1:])]


def slope(xs, ys) -> float:
    """Наклон прямой по методу наименьших квадратов (как np.polyfit(xs, ys, 1)[0])."""
    xs, ys = list(xs), list(ys)
    mx, my = mean(xs), mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0

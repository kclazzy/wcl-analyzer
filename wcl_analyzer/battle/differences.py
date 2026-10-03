"""3–5 главных отличий от топа, разрыв DPS и короткая сводка.

Отличия берутся из находок сравнения (compare.py) после шумового порога, плюс движение.
Число потерянного DPS показывается только там, где compare.py его оценил расчётом;
иначе фактор — «возможная причина», без цифры.
"""
from __future__ import annotations

from .timeline import sec

LEVEL = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}
SECTION_FILTER = {"Бурст": "burst", "Кулдауны": "cooldowns", "Зелья": "burst", "Защита": "defensives",
                  "Смерти": "errors", "Простой": "errors", "Ротация": "errors", "Проки": "errors",
                  "Ресурс": "errors", "Время действия": "errors", "Опенер": "burst", "Механики": "mechanics",
                  "Экипировка": "errors", "Движение": "movement"}


def _level(f) -> str:
    lv = LEVEL.get(f.impact_level)
    if lv:
        return lv
    # влияние не оценено: уровень по нормированному отклонению
    return "high" if f.severity >= 6 else "medium" if f.severity >= 3 else "low"


def _value(f) -> str:
    """Короткое значение отличия: «+10 с», «−6%», «−1 каст»."""
    if f.my is None or f.ref is None:
        return ""
    d = f.my - f.ref
    u = f.unit or ""
    if u.startswith("доля"):
        return f"{d * 100:+.0f}%".replace("-", "−")
    if u.startswith("с"):
        return sec(d)
    if u in ("кастов", "применений", "раз", "проков"):
        return f"{d:+.0f} {u}".replace("-", "−")
    return ""


def top_differences(r, mv: dict, limit: int = 5) -> list[dict]:
    cands = []
    for f in r.findings:
        if f.noise or f.context:
            continue
        lv = _level(f)
        if lv == "low":
            continue
        cands.append({"level": lv, "section": f.section, "text": f.title, "value": _value(f),
                      "do": f.focus or "", "gain": f"≈ +{f.impact:.1%} DPS".replace(".", ",") if f.impact else "",
                      "filter": SECTION_FILTER.get(f.section, "errors"),
                      "_rank": (2 if lv == "high" else 1, f.impact or 0, f.severity)})
    d = mv.get("diff")
    if mv.get("available") and d is not None and mv.get("top_total"):
        if d >= 2 and d >= 0.15 * mv["top_total"]:
            lv = "high" if d >= 0.5 * mv["top_total"] and d >= 5 else "medium"
            cands.append({"level": lv, "section": "Движение",
                          "text": f"Больше движения, чем у топа: {sec(mv['total'], False)} против {sec(mv['top_total'], False)}",
                          "value": sec(d), "do": "Меньше лишних перемещений: заранее вставать туда, где не придётся бежать",
                          "gain": "", "filter": "movement", "_rank": (2 if lv == "high" else 1, 0, d)})
    cands.sort(key=lambda c: c["_rank"], reverse=True)
    out, seen = [], set()
    for c in cands:  # по одному отличию на раздел — чтобы пять пунктов не были об одном
        if c["section"] in seen:
            continue
        seen.add(c["section"])
        out.append({k: v for k, v in c.items() if k != "_rank"})
        if len(out) >= limit:
            break
    return out


def dps_gap(r, diffs: list[dict]) -> dict:
    my, top = r.me.dps, r.ref.agg["dps"]["median"]
    return {"my": round(my), "top": round(top) if top else None,
            "gap": round((my - top) / top, 4) if top else None,
            # возможные причины — разделы главных отличий, без приписанных цифр
            "contributors": list(dict.fromkeys(d["section"] for d in diffs))}


def summary(r, mv: dict, diffs: list[dict]) -> list[str]:
    lines = []
    g = dps_gap(r, diffs)
    if g["gap"] is not None:
        lines.append(f"DPS {_n(g['my'])} против {_n(g['top'])} у медианы топа ({_pct(g['gap'])}).")
    if diffs:
        lines.append(f"Главное отличие: {diffs[0]['text'][0].lower()}{diffs[0]['text'][1:]}.")
    if mv.get("available") and mv.get("top_total") is not None:
        lines.append(f"Движение: {sec(mv['total'], False)} за бой, у топа {sec(mv['top_total'], False)}"
                     f" ({sec(mv['diff'])}); значимых перемещений — {len(mv.get('moves') or [])}.")
    elif not mv.get("available"):
        lines.append("Координат игрока в логе нет — карта движения недоступна.")
    return lines[:5]


def _n(v) -> str:
    return f"{v:,.0f}".replace(",", " ")


def _pct(v) -> str:
    return f"{v:+.1%}".replace(".", ",").replace("-", "−")

"""План рейдовых сейвов на всех боссов отчёта: одна кнопка — план и заметка MRT на каждого босса.

Для каждого босса (и сложности) берётся один бой: последний килл, а если киллов нет — лучший пулл
(босс снесён ниже всего). Дальше — тот же разбор, что у «Разбор рейда: урон, сейвы, смерти»: состав и его кулдауны,
пики урона, лучшие киллы этого босса, фазы, план с перезарядкой и заметка MRT.
"""
from __future__ import annotations

from .compare import _fmt_t
from .config import DIFFICULTY_NAMES, SITE_URL
from .logs import parse_report_url


SAVES_DIFFICULTIES = (4, 5)   # героическая и эпохальная: на обычной и в LFR план сейвов не нужен


def pick_fights(report: dict) -> list[dict]:
    """По одному бою на босса и сложность (только героическая и эпохальная), в порядке боссов в отчёте."""
    groups: dict = {}
    for f in sorted(report.get("fights") or [], key=lambda x: float(x.get("startTime") or 0)):
        if not int(f.get("encounterID") or 0) or int(f.get("difficulty") or 0) not in SAVES_DIFFICULTIES:
            continue
        groups.setdefault((int(f["encounterID"]), int(f.get("difficulty") or 0)), []).append(f)
    out = []
    for (_enc, _diff), fs in groups.items():
        kills = [f for f in fs if f.get("kill")]
        best = kills[-1] if kills else min(fs, key=lambda f: (f.get("fightPercentage") is None,
                                                             f.get("fightPercentage") or 100.0,
                                                             -(float(f["endTime"]) - float(f["startTime"]))))
        out.append({"fight": best, "pulls": len(fs), "kills": len(kills)})
    return out


def run_raid_saves(client, url: str, log=print, progress=lambda x: None, talent_data=None,
                   save_talents: bool = True, avoidable: set | None = None) -> dict:
    from .raid import run_raid
    from .raid_top import mrt_note

    code, _, _ = parse_report_url(url)
    report = client.report(code)
    chosen = pick_fights(report)
    if not chosen:
        raise LookupError("В отчёте нет боёв с боссами на героической или эпохальной сложности — "
                          "план сейвов составляется только для них")
    other = sum(1 for f in report.get("fights") or [] if int(f.get("encounterID") or 0)
                and int(f.get("difficulty") or 0) not in SAVES_DIFFICULTIES)
    if other:
        log(f"Бои на обычной сложности и в поиске рейда пропущены: {other}.")
    log(f"Боссов в отчёте: {len(chosen)}. На каждого — план сейвов по лучшему бою и лучшим киллам топа.")
    bosses, skipped = [], []
    for i, c in enumerate(chosen):
        f = c["fight"]
        diff = DIFFICULTY_NAMES.get(int(f.get("difficulty") or 0), "")
        state = "килл" if f.get("kill") else f"вайп, лучший пулл ({_pct(f)})"
        log(f"[{i + 1}/{len(chosen)}] {f.get('name')} ({diff}): {state}, {_fmt_t((float(f['endTime']) - float(f['startTime'])) / 1000)}")
        base = i / len(chosen)
        try:
            R = run_raid(client, url, int(f["id"]), log=lambda m: log("    " + m), avoidable=avoidable,
                         talent_data=talent_data, save_talents=save_talents, mythic=False,
                         progress=lambda x, b=base: progress(b + x / len(chosen)))
        except Exception as e:  # noqa: BLE001 — один босс не должен ронять остальные
            log(f"    пропущен: {e}")
            skipped.append({"boss": f.get("name", ""), "difficulty": diff, "reason": str(e)})
            continue
        X, I = R["extras"], R["info"]
        V = X.get("vs_top") or {}
        plan = X.get("plan") or []
        bosses.append({
            "boss": I["boss"], "difficulty": I["difficulty"], "fight_id": I["fight_id"], "kill": I["kill"],
            "boss_pct": I.get("boss_pct"), "duration": I["duration"], "url": I["url"], "phases": I.get("phases") or [],
            "pulls": c["pulls"], "kills": c["kills"], "size": I.get("size"), "healers": I.get("healers"),
            "plan": plan, "mrt": mrt_note(plan, I["boss"]),
            "saves_brief": X.get("saves_brief") or [],
            "top_kills": V.get("kills") or [], "top_cover": V.get("top_cover"), "my_cover": V.get("my_cover"),
            "roster": len({c["player"] for c in X.get("roster_cds") or []}),
        })
        progress((i + 1) / len(chosen))
    if not bosses:
        raise LookupError("Не удалось составить план ни для одного босса: " + "; ".join(s["reason"] for s in skipped))
    return {"mode": "saves",
            "info": {"code": code, "title": report.get("title", ""), "zone": (report.get("zone") or {}).get("name", ""),
                     "url": f"{SITE_URL}/reports/{code}", "bosses": len(bosses),
                     "boss": (report.get("zone") or {}).get("name") or report.get("title", ""),
                     "difficulty": ", ".join(dict.fromkeys(b["difficulty"] for b in bosses)),
                     "demo": code.startswith("DEMO")},
            "bosses": bosses, "skipped": skipped}


def _pct(f: dict) -> str:
    p = f.get("fightPercentage")
    return f"босс на {p:.1f}%".replace(".", ",") if p is not None else "без процента"

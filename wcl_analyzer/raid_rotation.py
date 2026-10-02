"""Сравнение ротации всего рейда: каждый DPS боя против топа своего спека.

Эталон собирается один раз на спек: если в рейде два Frost Mage, топ скачивается один раз.
Танки и лекари пропускаются — сравнение ротации рассчитано на урон.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median

from .compare import _fmt_t, _pct, compare
from .names_ru import spec_ru
from .reference import build_reference

# Группы находок для общей картины по рейду (ключ находки → понятное название)
GROUPS = [
    ("death", "Смерти"),
    ("gcd", "Простой между действиями больше, чем у топа"),
    ("rot:", "Не хватает кастов ключевых способностей"),
    ("proc:", "Потерянные проки"),
    ("prio:", "Не тот приоритет при проках и ресурсе"),
    ("cdn:", "Меньше применений кулдаунов, чем у топа"),
    ("cdt:", "Кулдауны нажаты не тогда, когда их жмёт топ"),
    ("burst:", "Окна бурста собраны не так, как у топа"),
    ("vuln:", "Бурст мимо окон уязвимости босса"),
    ("pot:", "Зелья выпиты не так, как у топа"),
    ("up:", "Время действия своих эффектов ниже топа"),
    ("res:", "Потери ресурса"),
    ("opener", "Опенер отличается от топа"),
    ("def", "Защитные способности не под опасные механики"),
]


def _group(key: str) -> str | None:
    for prefix, label in GROUPS:
        if key == prefix or key.startswith(prefix):
            return label
    return None


def analyze_players(players: list, refs_for, overrides: dict | None = None, log=print,
                    progress=lambda x: None) -> tuple[list[dict], list]:
    """players: [{name, cls, spec, log: PlayerLog | None, error}].
    refs_for(me) -> (tops, label) — эталон для игрока (кэшируется вызывающим кодом).
    Возвращает строки результата и список CompareResult (для Excel)."""
    rows, results = [], []
    n = len(players)
    for i, p in enumerate(players):
        progress(0.8 + 0.17 * i / max(1, n))
        me = p.get("log")
        row = {"name": p["name"], "cls": p["cls"], "spec": p["spec"], "id": p.get("id"),
               "rank": p.get("rank"), "ilvl": p.get("ilvl")}
        if me is None:
            rows.append({**row, "error": p.get("error") or "Лог не загружен"})
            continue
        try:
            tops, label = refs_for(me)
            log(f"Сравниваю: {me.name} ({spec_ru(me.cls, me.spec)}) — {i + 1} из {n}")
            ref = build_reference(tops, overrides, label=label, me=me)
            r = compare(me, ref)
        except Exception as e:  # noqa: BLE001 — один игрок не должен ронять весь рейд
            rows.append({**row, "error": str(e) or type(e).__name__})
            continue
        b = r.brief
        visible = [f for f in r.findings if not f.noise and not f.context
                   and (f.impact is None or f.impact >= 0.01)]
        rows.append({**row, "dps": me.dps, "ref_dps": ref.agg["dps"]["median"], "gap": b["gap"],
                     "ref_label": ref.label, "ref_n": ref.n, "reliability": b["reliability"],
                     "actions": b["actions"], "context": b["context"], "deaths": b["deaths"],
                     "explained": b["explained"],
                     "groups": sorted({g for g in (_group(f.key) for f in visible) if g}),
                     "result": r})
        results.append(r)
    return rows, results


def summarize(rows: list[dict], skipped: list[dict]) -> list[str]:
    """Выжимка по рейду: общий разрыв, кто дальше всех от топа, общие для многих проблемы."""
    ok = [r for r in rows if "error" not in r]
    out = []
    if ok:
        gaps = [r["gap"] for r in ok]
        out.append(f"Разобрано DPS: {len(ok)}. Медиана отставания от топа своего спека: "
                   f"{_pct(max(0.0, -median(gaps)))}" + (f"; выше медианы топа — {sum(1 for g in gaps if g >= 0)}"
                                                       if any(g >= 0 for g in gaps) else ""))
        worst = sorted(ok, key=lambda r: r["gap"])[:3]
        out.append("Дальше всего от топа: " + ", ".join(
            f"{r['name']} ({_pct(-r['gap'])} ниже)" for r in worst if r["gap"] < 0))
        cnt = Counter(g for r in ok for g in r["groups"])
        common = [(g, c) for g, c in cnt.most_common() if c >= 2][:3]
        for g, c in common:
            names = [r["name"] for r in ok if g in r["groups"]]
            out.append(f"{g} — у {c} из {len(ok)}: {', '.join(names[:5])}{'…' if len(names) > 5 else ''}")
        ctx = Counter(c for r in ok for c in r["context"] if c.startswith("На боссе"))
        for c, k in ctx.most_common(1):
            if k >= max(2, len(ok) // 2):
                out.append(c.replace("На боссе почти не было", "На боссе не хватало рейдового дебаффа")
                           + " — минус для всего рейда")
    errs = [r for r in rows if "error" in r]
    if errs:
        out.append("Не удалось разобрать: " + ", ".join(f"{r['name']} ({r['error'][:60]})" for r in errs[:4]))
    if skipped:
        out.append(f"Танки и лекари не сравниваются ({len(skipped)}): сравнение ротации рассчитано на урон.")
    return [x for x in out if x and not x.endswith(": ")]


def _mythic_gaps(client, rows: list[dict], players: list[dict], top_n: int, log) -> None:
    """Отставание каждого DPS от эпохального топа своего спека — по DPS из рейтинга, без скачивания логов
    (один запрос на спек). Полное сравнение с эпохальным топом — в разборе отдельного игрока."""
    logs = {p["id"]: p.get("log") for p in players}
    cache: dict = {}
    for r in rows:
        me = logs.get(r.get("id"))
        if "error" in r or me is None:
            continue
        key = (me.encounter_id, me.cls, me.spec)
        if key not in cache:
            try:
                enc = client.rankings(me.encounter_id, me.cls, me.spec, 5, 1)
                amounts = [float(x["amount"]) for x in ((enc.get("characterRankings") or {}).get("rankings") or [])[:top_n]
                           if x.get("amount")]
                cache[key] = median(amounts) if amounts else None
            except Exception as e:  # noqa: BLE001
                log(f"  Эпохальный рейтинг {spec_ru(me.cls, me.spec)} недоступен: {e}")
                cache[key] = None
        m = cache[key]
        if m:
            r["mythic_dps"], r["mythic_gap"] = m, me.dps / m - 1
    if cache:
        log(f"Эпохальный топ по рейтингу: {sum(1 for v in cache.values() if v)} из {len(cache)} спеков")


def report_points(client, log) -> None:
    """Пишет в журнал, сколько запросов сделано и сколько очков API осталось в этом часе."""
    left = client.points_left() if hasattr(client, "points_left") else None
    made = getattr(client, "requests_made", None)
    if made is not None:
        log(f"Запросов к Warcraft Logs: {made}, из сохранённых: {getattr(client, 'cache_hits', 0)}"
            + (f"; очков API осталось в этом часе: {left:,.0f}".replace(",", " ") if left is not None else ""))


PICK_LABEL = {"rank": "процентиль", "ilvl": "процентиль по уровню предметов (ilvl%)"}


def parse_pick(pick) -> tuple[str, float] | None:
    """«rank:50» → ("rank", 50): разбирать только игроков с процентилем не выше 50%."""
    if not pick or not isinstance(pick, str) or ":" not in pick:
        return None
    by, _, lim = pick.partition(":")
    try:
        v = float(lim)
    except ValueError:
        return None
    return (by, v) if by in PICK_LABEL and 0 < v < 100 else None


def pick_players(dps: list[dict], pick, log=print) -> tuple[list[dict], list[dict]]:
    """Отбор DPS по процентилю WCL. Возвращает (кого разбирать, кто не прошёл отбор)."""
    rule = parse_pick(pick)
    if not rule:
        return dps, []
    by, lim = rule
    known = [p for p in dps if p.get(by) is not None]
    if not known:
        log(f"Отбор по {PICK_LABEL[by]} невозможен: у этого боя нет рейтингов WCL (их нет у вайпов). Разбираю всех DPS.")
        return dps, []
    keep = [p for p in known if p[by] <= lim]
    out = [p for p in dps if p not in keep]
    log(f"Отбор: {PICK_LABEL[by]} ≤ {lim:g}% — {len(keep)} из {len(dps)} DPS"
        + (f"; без рейтинга пропущены: {', '.join(p['name'] for p in dps if p.get(by) is None)}"
           if len(known) < len(dps) else ""))
    if not keep:
        raise LookupError(f"Нет DPS с {PICK_LABEL[by]} ≤ {lim:g}% в этом бою — выберите порог выше")
    return keep, out


def run_raid_rotation(client, url: str, fight=None, top_n: int = 10, log=print, progress=lambda x: None,
                      overrides: dict | None = None, max_age_s: float | None = None, save: bool = False,
                      pick: str | None = None, mythic: bool = True) -> dict:
    from .collect import collect_reference, inspect_report, load_my_log
    insp = inspect_report(client, url, fight)
    dps = [p for p in insp["players"] if p["role"] == "DPS"]
    skipped = [p for p in insp["players"] if p["role"] != "DPS"]
    if not dps:
        raise LookupError("В этом бою не найдено DPS-игроков")
    dps, not_picked = pick_players(dps, pick, log)
    log(f"Бой {insp['fight']}: {len(dps)} DPS. Загружаю их логи…")
    from concurrent.futures import ThreadPoolExecutor

    from .collect import parallel_workers

    def load(p):
        try:
            return {**p, "log": load_my_log(client, url, insp["fight"], actor_id=p["id"])}
        except Exception as e:  # noqa: BLE001
            return {**p, "log": None, "error": str(e)}

    players = []
    with ThreadPoolExecutor(max_workers=parallel_workers(client)) as pool:
        for i, x in enumerate(pool.map(load, dps)):  # порядок игроков сохраняется
            progress(0.05 + 0.25 * (i + 1) / len(dps))
            players.append(x)
            me = x.get("log")
            log(f"  {me.name}: {spec_ru(me.cls, me.spec)}, {me.dps:,.0f} DPS".replace(",", " ") if me
                else f"  {x['name']}: не удалось загрузить — {x.get('error')}")
    report_points(client, log)

    specs = {(x["log"].encounter_id, x["log"].cls, x["log"].spec, x["log"].difficulty)
             for x in players if x.get("log")}
    log(f"Собираю эталоны: {len(specs)} {'спек' if len(specs) == 1 else 'спеков'}, топ-{top_n} каждого. "
        "Первый раз это долго: на каждый спек нужно скачать бои топа.")
    cache: dict = {}
    first = next(x["log"] for x in players if x.get("log")) if any(x.get("log") for x in players) else None
    total = max(1, len(specs) * top_n)
    done = [0]

    def quiet(m: str) -> None:
        # Каждый скачанный лог топа двигает прогресс: это самая долгая часть разбора рейда
        if m.strip().startswith("["):
            done[0] += 1
            progress(0.3 + 0.5 * min(1.0, done[0] / total))
        log("    " + m.strip().replace("[", "(").replace("]", ")"))

    for k, key in enumerate(sorted(specs)):
        done[0] = max(done[0], k * top_n)
        enc, cls, spec, diff = key
        log(f"  Эталон {k + 1} из {len(specs)}: {spec_ru(cls, spec)}")
        try:
            cache[key] = collect_reference(client, enc, cls, spec, diff, top_n=top_n,
                                           duration=first.duration if first else None, log=quiet,
                                           max_age_s=max_age_s, save=save)
        except Exception as e:  # noqa: BLE001
            cache[key] = e
            log(f"  {spec_ru(cls, spec)}: эталон не собран — {e}")

    def refs_for(me):
        v = cache[(me.encounter_id, me.cls, me.spec, me.difficulty)]
        if isinstance(v, Exception):
            raise v
        return v

    report_points(client, log)
    rows, results = analyze_players(players, refs_for, overrides, log, progress)
    if mythic and first and first.difficulty and int(first.difficulty) != 5:
        _mythic_gaps(client, rows, players, top_n, log)
    m = first
    brief = summarize(rows, skipped)
    mg = [r["mythic_gap"] for r in rows if r.get("mythic_gap") is not None]
    if mg:
        brief.insert(1, f"К эпохальному топу своего спека (по рейтингу, медиана топ-{top_n}): "
                        f"медиана отставания {_pct(max(0.0, -median(mg)))}")
    rule = parse_pick(pick)
    if rule and not_picked:
        brief.insert(0, f"Разобраны только DPS с {PICK_LABEL[rule[0]]} ≤ {rule[1]:g}%: {len(dps)} из {len(dps) + len(not_picked)}")
    return {"rows": rows, "results": results, "skipped": skipped, "not_picked": [p["name"] for p in not_picked],
            "brief": brief, "pick": pick if rule else None,
            "info": {"boss": m.encounter_name if m else "", "difficulty": m.difficulty_name if m else "",
                     "duration": _fmt_t(m.duration) if m else "", "kill": m.kill if m else None,
                     "fight_id": insp["fight"], "code": insp["code"], "top_n": top_n,
                     "url": f"https://www.warcraftlogs.com/reports/{insp['code']}#fight={insp['fight']}",
                     "demo": False}}


def run_demo(top_n: int = 25, log=print, progress=lambda x: None) -> dict:
    from .demo import demo_raid_players
    tops, players = demo_raid_players(top_n)
    for i, p in enumerate(players):  # демо-процентили, чтобы было видно, как они выглядят
        p.setdefault("rank", float((17 + 23 * i) % 97))
        p.setdefault("ilvl", float((29 + 31 * i) % 97))
    log(f"Демо: {len(players)} DPS, эталон топ-{top_n}.")
    rows, results = analyze_players(players, lambda me: (tops, f"топ-{top_n}"), None, log, progress)
    m = players[0]["log"]
    skipped = [{"name": n} for n in ("Гронвальд", "Сайрена", "Элария", "Таргун")]
    return {"rows": rows, "results": results, "skipped": skipped, "brief": summarize(rows, skipped),
            "info": {"boss": m.encounter_name, "difficulty": m.difficulty_name, "duration": _fmt_t(m.duration),
                     "kill": True, "fight_id": 1, "code": m.report_code, "top_n": top_n,
                     "url": m.url, "demo": True}}

"""Сбор данных через API: мой лог по ссылке и эталон Top N."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .api import WCLClient, WCLError


def parallel_workers(client) -> int:
    """Сколько логов качать одновременно: у настоящего клиента — по его лимиту, у тестовых — 1."""
    sem = getattr(client, "_sem", None)
    return max(1, getattr(sem, "_initial_value", 1)) if sem is not None else 1
from .logs import PlayerLog, build_player_log, fetch_raw, find_player, parse_report_url


def _pick_fight(report: dict, fight_arg: str | int | None) -> dict:
    fights = [f for f in report["fights"] if f.get("encounterID")]
    if not fights:
        raise LookupError("В отчёте нет боёв с боссами")
    if fight_arg not in (None, "last"):
        fid = int(fight_arg)
        match = [f for f in fights if int(f["id"]) == fid]
        if not match:
            raise LookupError(f"Бой {fid} не найден. Бои с боссами: "
                              + ", ".join(f"{f['id']} {f['name']}{' (килл)' if f.get('kill') else ''}" for f in fights))
        return match[0]
    kills = [f for f in fights if f.get("kill")]
    return (kills or fights)[-1]


def shared_cache(client):
    """Общие данные боя (смерти, касты босса, экипировка, дебаффы на боссе) — один раз на бой,
    сколько бы игроков этого боя ни разбиралось. Безопасно для потоков."""
    import threading
    from .logs import fetch_shared
    store: dict = {}
    lock = threading.Lock()

    def get(report: dict, fight: dict) -> dict:
        key = (report["code"], int(fight["id"]))
        with lock:
            if key not in store:
                store[key] = fetch_shared(client, report, fight)
            return store[key]
    return get


def load_my_log(client: WCLClient, url: str, fight: str | int | None = None,
                player: str | None = None, actor_id: int | None = None,
                shared: dict | None = None, damage_events: bool = True) -> PlayerLog:
    """Лог игрока. shared — общие данные боя (logs.fetch_shared), когда разбираются многие игроки одного боя;
    damage_events=False — без событий нанесённого урона (экономия лимита: нет оценки прироста от кулдаунов)."""
    code, url_fight, url_source = parse_report_url(url)
    report = client.report(code)
    f = _pick_fight(report, fight if fight is not None else url_fight)
    if actor_id is None and not player:
        actor_id = url_source
    actor, cls, spec = find_player(client, report, f, name=player, actor_id=actor_id)
    if callable(shared):  # общие данные боя: функция бой → данные (кэш на стороне вызывающего)
        shared = shared(report, f)
    elif shared is None and hasattr(client, "events_multi"):
        # общие данные боя всегда отдельным запросом: тогда запросы одинаковы во всех режимах
        # и уже скачанное (кэш) переиспользуется между «Разобрать игрока», ротацией рейда и всеми боссами
        from .logs import fetch_shared
        shared = fetch_shared(client, report, f)
    raw = fetch_raw(client, report, f, int(actor["id"]), with_damage_events=damage_events, shared=shared)
    return ensure_talents(client, build_player_log(report, f, actor, raw, spec=spec, cls=cls), gear_names=True)


def collect_reference(client: WCLClient, encounter_id: int, cls: str, spec: str, difficulty: int,
                      top_n: int = 25, duration: float | None = None,
                      log=print, force: bool = False, meta: dict | None = None,
                      uid: str = "local", max_age_s: float | None = None,
                      save: bool = True) -> tuple[list[PlayerLog], str]:
    """Top N по боссу и спеку; при заданной длительности — сначала бои ±10%, затем ±20%.

    force=True скачивает рейтинг заново (бои игроков, уже бывшие в кэше, не перекачиваются).
    В meta записываются дата рейтинга и имя босса; эталон сохраняется в список эталонов."""
    from . import settings
    from .config import DIFFICULTY_NAMES
    if not difficulty:
        # Без сложности Warcraft Logs отдаёт рейтинг самой высокой (эпохальной) — сравнение было бы нечестным
        raise LookupError("Не удалось определить сложность боя: эталон не собран, чтобы не сравнить с чужой сложностью")
    diff_name = DIFFICULTY_NAMES.get(int(difficulty), str(difficulty))
    candidates: list[dict] = []
    page = 1
    fetched_at, boss = None, ""
    while len(candidates) < max(top_n * 4, 50) and page <= 8:
        try:
            enc = client.rankings(encounter_id, cls, spec, difficulty, page, force=force,
                                  max_age_s=max_age_s if max_age_s is not None else
                                  settings.max_age_s(getattr(getattr(client, "cache", None), "path", None), uid))
        except TypeError:  # клиенты без поддержки force (тесты)
            enc = client.rankings(encounter_id, cls, spec, difficulty, page)
        fetched_at = fetched_at or enc.get("_fetched_at")
        boss = boss or enc.get("name", "")
        data = enc.get("characterRankings") or {}
        candidates.extend(data.get("rankings") or [])
        if not data.get("hasMorePages"):
            break
        page += 1
    for i, rk in enumerate(candidates):
        rk["_rank"] = i + 1
    if not candidates:
        raise LookupError(f"Нет рейтинга для {cls} {spec} на боссе {encounter_id} (сложность {difficulty})")

    label = f"топ-{top_n}"
    chosen = candidates
    if duration:
        for w in (0.10, 0.20, None):
            pool = [rk for rk in candidates if w is None or abs(rk.get("duration", 0) / 1000 - duration) / duration <= w]
            if len(pool) >= min(top_n, 10) or w is None:
                chosen = pool
                label = f"топ-{top_n}" + (f" (длительность ±{w:.0%})" if w else "")
                break

    def load(rk):
        rep_info = rk.get("report") or {}
        code, fid = rep_info.get("code"), rep_info.get("fightID")
        if not code or rk.get("hidden"):
            return None, "скрытый лог"
        try:
            report = client.report(code)
            fight = next(f for f in report["fights"] if int(f["id"]) == int(fid))
            # Проверка: бой топа той же сложности и того же босса, что и ваш
            fd = int(fight.get("difficulty") or 0)
            if fd and fd != int(difficulty):
                return None, f"другая сложность ({DIFFICULTY_NAMES.get(fd, fd)}, нужна {diff_name})"
            if fight.get("encounterID") and int(fight["encounterID"]) != int(encounter_id):
                return None, "другой босс"
            actor = next(a for a in report["masterData"]["actors"]
                         if a["name"] == rk.get("name") and a.get("type") not in ("NPC", "Pet"))
            raw = fetch_raw(client, report, fight, int(actor["id"]))
            pl = ensure_talents(client, build_player_log(report, fight, actor, raw, spec=spec, cls=cls,
                                                         dps=rk.get("amount"), rank=rk["_rank"]))
            if not pl.ilvl and rk.get("bracketData"):
                pl.ilvl = float(rk["bracketData"])
            return pl, None
        except (WCLError, StopIteration, KeyError) as e:
            return None, str(e)

    # Логи топа качаются параллельно (число одновременных запросов ограничивает клиент),
    # но в эталон попадают строго в порядке рейтинга
    logs: list[PlayerLog] = []
    queue = [rk for rk in chosen if not rk.get("hidden") and (rk.get("report") or {}).get("code")]
    workers = parallel_workers(client)
    pos = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while len(logs) < top_n and pos < len(queue):
            need = top_n - len(logs)
            batch = queue[pos:pos + need + min(2, need)]
            pos += len(batch)
            for rk, (pl, err) in zip(batch, pool.map(load, batch)):
                if pl is None:
                    log(f"  пропущен #{rk['_rank']} {rk.get('name')}: {err}")
                elif len(logs) < top_n:
                    logs.append(pl)
                    log(f"  [{len(logs)}/{top_n}] #{rk['_rank']} {pl.name}: {pl.dps:,.0f} DPS, {pl.duration:.0f} с")
    if not logs:
        raise LookupError("Не удалось загрузить ни одного эталонного лога")
    import time as _time
    info = {"encounter_id": encounter_id, "boss": boss or logs[0].encounter_name, "cls": cls, "spec": spec,
            "difficulty": difficulty, "top_n": top_n, "duration": duration, "label": label,
            "n_logs": len(logs), "collected_at": fetched_at or _time.time(), "site": getattr(client, "site", "www")}
    if meta is not None:
        meta.update(info)
    cache = getattr(client, "cache", None)
    if save and cache is not None and getattr(cache, "path", None):
        settings.save_ref(cache.path, uid=uid, **info)  # список эталонов для командной строки
    return logs, label


def ensure_talents(client, pl, gear_names: bool = False):
    """Если в событиях боя талантов нет — берём их из сведений об игроках (запасной источник).
    gear_names=True — ещё и названия предметов и зачарований (в событиях боя их нет)."""
    need_gear = gear_names and pl.gear and not all(g.get("name") for g in pl.gear)
    if (pl.talent_tree and not need_gear) or not hasattr(client, "player_details"):
        return pl
    try:
        from .logs import gear_from_details, mark_trinket_spells, talents_from_details
        details = client.player_details(pl.report_code, pl.fight_id, combatant=True)
        if not pl.talent_tree:
            pl.talent_tree = talents_from_details(details, pl.actor_id)
        if need_gear or not pl.gear:
            gear_from_details(pl, details)
            mark_trinket_spells(pl)
    except Exception:  # noqa: BLE001 — без талантов разбор всё равно работает (TypeError — у тестовых клиентов)
        pass
    return pl


def player_percentiles(r) -> dict:
    """Рейтинги боя WCL → {имя игрока: {"rank": процентиль, "ilvl": процентиль по уровню предметов}}."""
    out: dict = {}
    data = r.get("data", r) if isinstance(r, dict) else r
    if isinstance(data, dict):
        data = [data]
    for fight in data or []:
        for role in ((fight or {}).get("roles") or {}).values():
            for c in (role or {}).get("characters") or []:
                if c.get("rankPercent") is None:
                    continue
                v = {"rank": float(c["rankPercent"]),
                     "ilvl": float(c["bracketPercent"]) if c.get("bracketPercent") is not None else None}
                # В рейтингах id — номер персонажа на сайте, а не номер игрока в отчёте:
                # сопоставляем по имени, id — только запасной вариант
                if c.get("name"):
                    out[str(c["name"])] = v
                if c.get("id") is not None:
                    out.setdefault(int(c["id"]), v)
    return out


def inspect_report(client: WCLClient, url: str, fight: str | int | None = None) -> dict:
    """Бои и игроки отчёта — для выбора в интерфейсе до запуска анализа."""
    from .config import DIFFICULTY_NAMES
    code, url_fight, url_source = parse_report_url(url)
    report = client.report(code)
    f = _pick_fight(report, fight if fight is not None else url_fight)
    details = client.player_details(code, int(f["id"]))
    players = []
    for role, role_ru in (("dps", "DPS"), ("healers", "Лекарь"), ("tanks", "Танк")):
        for p in details.get(role, []) or []:
            specs = p.get("specs") or []
            spec = specs[0].get("spec") if specs and isinstance(specs[0], dict) else (specs[0] if specs else "")
            players.append({"id": int(p["id"]), "name": p["name"], "cls": p.get("type", ""),
                            "spec": spec, "role": role_ru})
    ranks = {}
    if f.get("kill") and hasattr(client, "report_rankings"):  # у вайпов рейтингов нет
        try:
            ranks = player_percentiles(client.report_rankings(code, int(f["id"])))
        except Exception:  # noqa: BLE001 — без процентилей выбор боя всё равно работает
            ranks = {}
    for p in players:
        r = ranks.get(p["name"]) or ranks.get(p["id"]) or {}
        p["rank"], p["ilvl"] = r.get("rank"), r.get("ilvl")
    fights = [{"id": int(x["id"]), "name": x["name"], "kill": bool(x.get("kill")),
               "difficulty": DIFFICULTY_NAMES.get(int(x.get("difficulty") or 0), str(x.get("difficulty"))),
               "duration": (float(x["endTime"]) - float(x["startTime"])) / 1000,
               "percent": x.get("fightPercentage")}
              for x in report["fights"] if x.get("encounterID")]
    return {"code": code, "title": report.get("title", ""), "zone": (report.get("zone") or {}).get("name", ""),
            "fights": fights, "fight": int(f["id"]), "players": players,
            "source": url_source if any(p["id"] == url_source for p in players) else None}


def refresh_refs(client, uid: str = "local", keys: list[str] | None = None,
                 only_stale: bool = False, log=print) -> int:
    """Командная строка: обновить эталоны из локального списка (файл кэша на этом компьютере)."""
    from . import settings
    from .config import cache_path
    rows = settings.list_refs(cache_path(), uid)
    if keys:
        rows = [r for r in rows if r["key"] in keys]
    if only_stale:
        rows = [r for r in rows if r["stale"]]
    for i, r in enumerate(rows):
        log(f"[{i}/{len(rows)}] {r['boss']}: {r['spec']} {r['cls']}, топ-{r['top_n']}…")
        collect_reference(client, r["encounter_id"], r["cls"], r["spec"], r["difficulty"], top_n=r["top_n"],
                          duration=r["duration"], force=True, log=lambda m: log("  " + m.strip()), uid=uid)
    return len(rows)

"""Командная строка: python -m wcl_analyzer <команда> ..."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REF_MODES = {"top1": 1, "top3": 3, "top10": 10}


def _safe(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name).strip("_")[:60] or "report"


def _client():
    from .api import Cache, WCLClient
    from .config import cache_path, credentials
    cid, secret = credentials()
    return WCLClient(cid, secret, Cache(cache_path()))


def _run_job(runner, params: dict, creds) -> dict:
    """Командная строка запускает те же задачи, что и приложение (web.py), — один код на всё."""
    job = {"id": "cli", "progress": 0.0, "log": [], "state": "running"}
    runner(job, params, creds, lambda m: print(m, flush=True))
    return job


def _save_excel(job: dict, out: str | None, key: str = "xlsx") -> Path | None:
    if not job.get(key):
        return None
    path = Path(out or job.get(key + "_name") or f"{key}.xlsx")
    path.write_bytes(job[key])
    return path.resolve()


def _creds():
    from .config import credentials
    return credentials()


def _print_brief(R: dict) -> None:
    B = R.get("brief") or {}
    print()
    if isinstance(B, dict):
        print(f"Надёжность сравнения: {B.get('reliability')}")
        print("Что сделать в следующем бою:")
        for i, act in enumerate(B.get("actions") or [], 1):
            print(f"  {i}. {act['do']} — {act['gain']}")
    else:
        for line in B:
            print(f"  • {line}")
    T = R.get("talents") or {}
    for line in T.get("summary") or ([T["error"]] if T.get("error") else []):
        print(f"  • {line}")


def cmd_demo(a):
    from . import web
    job = _run_job(web._run_player, {"demo": True}, None)
    _print_brief(job["result"])
    print(f"\nОтчёт Excel: {_save_excel(job, a.out)}")


def cmd_compare(a):
    from . import web
    from .collect import inspect_report
    creds = _creds()
    actor = None
    if a.player:  # имя персонажа → номер игрока в отчёте
        insp = inspect_report(web.CLIENT_FACTORY(creds), a.url, a.fight)
        actor = next((p["id"] for p in insp["players"] if p["name"].lower() == a.player.lower()), None)
        if actor is None:
            raise ValueError(f"В бою нет игрока {a.player}")
    job = _run_job(web._run_player, {"url": a.url, "fight": a.fight, "actor": actor, "ref": a.ref,
                                     "refresh": a.refresh, "against": a.against, "mythic": not a.no_mythic}, creds)
    R = job["result"]
    _print_brief(R)
    print(f"\nОтчёт Excel: {_save_excel(job, a.out)}")
    if R.get("alt"):
        print("Эпохальный топ:")
        _print_brief(R["alt"])
        print(f"Отчёт Excel (эпохальный топ): {_save_excel(job, None, 'xlsx_alt')}")


def cmd_guide(a):
    from .collect import collect_reference
    from .excel_report import write_reference_workbook
    from .metrics import load_overrides
    from .reference import build_reference
    client = _client()
    tops, label = collect_reference(client, a.encounter, a.cls, a.spec, a.difficulty, top_n=a.top)
    ref = build_reference(tops, load_overrides(a.meta), label=label)
    out = a.out or f"guide_{_safe(a.cls)}_{_safe(a.spec)}_{a.encounter}.xlsx"
    write_reference_workbook(ref, out)
    print(f"Гайд сохранён: {Path(out).resolve()}")


def cmd_bosses(a):
    client = _client()
    z = client.zone_encounters(a.zone)
    print(f"{z['name']} (zone {z['id']})")
    for d in z.get("difficulties") or []:
        print(f"  сложность {d['id']}: {d['name']}")
    for e in z["encounters"]:
        print(f"  {e['id']:>6}  {e['name']}")


def cmd_raid(a):
    from . import web
    if not a.demo and not a.url:
        raise ValueError("Укажите ссылку на отчёт или --demo")
    job = _run_job(web._run_raid, {"mode": "raid", "demo": a.demo, "url": a.url, "fight": a.fight,
                                   "mythic": not a.no_mythic}, None if a.demo else _creds())
    R = job["result"]
    print()
    for line in R.get("brief") or []:
        print(f"  • {line}")
    print("План сейвов на следующий пулл:")
    for r in (R.get("extras") or {}).get("plan") or []:
        picks = "; ".join(f"{p['cd']} — {p['player']}" for p in r.get("picks") or []) or "нет свободного кулдауна"
        print(f"  {r['time']} {r['mechanic']}: {picks}")
    print(f"\nОтчёт Excel: {_save_excel(job, a.out)}")


def cmd_raidrot(a):
    from . import web
    if not a.demo and not a.url:
        raise ValueError("Укажите ссылку на отчёт или --demo")
    job = _run_job(web._run_raid_rotation, {"mode": "raidrot", "demo": a.demo, "url": a.url, "fight": a.fight,
                                            "ref": a.ref, "pick": a.pick, "mythic": not a.no_mythic},
                   None if a.demo else _creds())
    print()
    for line in job["result"].get("brief") or []:
        print(f"  • {line}")
    print(f"\nОтчёт Excel: {_save_excel(job, a.out)}")


def cmd_refresh(a):
    from . import settings
    from .config import cache_path
    from .collect import refresh_refs
    rows = settings.list_refs(cache_path(), settings.LOCAL_UID)
    if a.list or not rows:
        if not rows:
            print("Сохранённых эталонов пока нет: они появляются после первого сравнения.")
        for r in rows:
            print(f"  {r['boss']}: {r['spec']} {r['cls']}, топ-{r['top_n']} — собран {r['age_days']:.1f} дн. назад"
                  + (" (устарел)" if r["stale"] else ""))
        return
    n = refresh_refs(_client(), settings.LOCAL_UID, only_stale=not a.all)
    print(f"Обновлено эталонов: {n}" + ("" if a.all else " (только устаревшие; все — с --all)"))


def cmd_ui(a):
    from .web import serve
    serve(port=a.port, open_browser=not a.no_browser, local_only=a.local_only, public=a.public)


def cmd_limit(a):
    info = _client().rate_limit()
    print(f"Потрачено {info['pointsSpentThisHour']:.1f} из {info['limitPerHour']} очков, "
          f"сброс через {info['pointsResetIn']} с")


def main(argv=None):
    p = argparse.ArgumentParser(prog="wcl_analyzer", description="Анализ ротации по Warcraft Logs")
    sub = p.add_subparsers(dest="cmd")

    u = sub.add_parser("ui", help="открыть интерфейс в браузере (по умолчанию)")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--no-browser", action="store_true")
    u.add_argument("--local-only", action="store_true", help="не открывать доступ с телефона")
    u.add_argument("--public", action="store_true",
                   help="режим сервиса в интернете: много пользователей, у каждого свой ключ API")
    u.set_defaults(func=cmd_ui)

    d = sub.add_parser("demo", help="прогон на синтетических данных, без ключа API")
    d.add_argument("--out", default="demo_report.xlsx")
    d.set_defaults(func=cmd_demo)

    c = sub.add_parser("compare", help="сравнить ваш лог с топом спека")
    c.add_argument("url", help="ссылка на отчёт WCL (можно с #fight=…&source=…)")
    c.add_argument("--fight", help="номер боя в отчёте (по умолчанию — последний килл)")
    c.add_argument("--player", help="имя персонажа (если в ссылке нет source=)")
    c.add_argument("--ref", choices=list(REF_MODES), default="top10", help="эталонная группа")
    c.add_argument("--against", help="сравнить с конкретным логом (ссылка) вместо топа")
    c.add_argument("--refresh", action="store_true", help="заново скачать рейтинг лучших логов")
    c.add_argument("--no-mythic", action="store_true", help="не сравнивать дополнительно с эпохальным топом")
    c.add_argument("--out")
    c.set_defaults(func=cmd_compare)

    g = sub.add_parser("guide", help="гайд эталона по боссу и спеку без своего лога")
    g.add_argument("--encounter", type=int, required=True)
    g.add_argument("--class", dest="cls", required=True, help="например Mage, DeathKnight")
    g.add_argument("--spec", required=True, help="например Frost, BeastMastery")
    g.add_argument("--difficulty", type=int, default=5, help="5 — эпохальный, 4 — героический, 3 — обычный")
    g.add_argument("--top", type=int, default=25)
    g.add_argument("--meta", default="spell_meta.json")
    g.add_argument("--out")
    g.set_defaults(func=cmd_guide)

    b = sub.add_parser("bosses", help="список боссов рейда")
    b.add_argument("--zone", type=int, default=53)
    b.set_defaults(func=cmd_bosses)

    rd = sub.add_parser("raid", help="разбор всего рейда в одном бою")
    rd.add_argument("url", nargs="?", help="ссылка на отчёт WCL")
    rd.add_argument("--fight", help="номер боя (по умолчанию — последний килл)")
    rd.add_argument("--demo", action="store_true", help="на демо-данных")
    rd.add_argument("--no-mythic", action="store_true", help="без лучших эпохальных киллов")
    rd.add_argument("--out")
    rd.set_defaults(func=cmd_raid)

    rr = sub.add_parser("raidrot", help="ротация всего рейда: каждый DPS против топа своего спека")
    rr.add_argument("url", nargs="?", help="ссылка на отчёт WCL")
    rr.add_argument("--fight")
    rr.add_argument("--demo", action="store_true")
    rr.add_argument("--ref", choices=list(REF_MODES), default="top10")
    rr.add_argument("--pick", help="отбор: rank:50 или ilvl:75 — только игроки с процентилем не выше")
    rr.add_argument("--no-mythic", action="store_true")
    rr.add_argument("--out")
    rr.set_defaults(func=cmd_raidrot)

    rf = sub.add_parser("refresh", help="обновить сохранённые эталоны (лучшие логи)")
    rf.add_argument("--all", action="store_true", help="все, а не только устаревшие")
    rf.add_argument("--list", action="store_true", help="только показать список")
    rf.set_defaults(func=cmd_refresh)

    sub.add_parser("limit", help="сколько очков API потрачено").set_defaults(func=cmd_limit)

    a = p.parse_args(argv)
    if not a.cmd:  # без команды — интерфейс
        a = p.parse_args(["ui"] + list(argv or []))
    try:
        a.func(a)
    except (LookupError, ValueError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

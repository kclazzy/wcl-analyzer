"""Командная строка: python -m wcl_analyzer <команда> ..."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REF_MODES = {"top1": 1, "top10": 10, "top25": 25, "top50": 50, "median25": 25}


def _safe(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name).strip("_")[:60] or "report"


def _client():
    from .api import Cache, WCLClient
    from .config import cache_path, credentials
    cid, secret = credentials()
    return WCLClient(cid, secret, Cache(cache_path()))


def cmd_demo(a):
    from .compare import compare
    from .demo import demo_logs
    from .excel_report import write_compare_workbook
    from .metrics import load_overrides
    from .reference import build_reference
    tops, me = demo_logs(n_top=a.top)
    ref = build_reference(tops, load_overrides(a.meta), label=f"топ-{a.top}", me=me)
    r = compare(me, ref)
    out = write_compare_workbook(r, a.out)
    _print_result(r, out)


def cmd_compare(a):
    from .collect import collect_reference, load_my_log
    from .compare import compare
    from .excel_report import write_compare_workbook
    from .logs import PlayerLog  # noqa: F401
    from .metrics import load_overrides
    from .reference import build_reference
    client = _client()
    print("Загружаю ваш лог…")
    me = load_my_log(client, a.url, a.fight, a.player)
    print(f"  {me.name}: {me.cls} {me.spec}, {me.encounter_name} ({me.difficulty_name}), "
          f"{me.duration:.0f} с, {me.dps:,.0f} DPS")
    if a.against:
        print("Загружаю лог для сравнения…")
        other = load_my_log(client, a.against, a.against_fight, a.against_player)
        tops, label = [other], f"Игрок {other.name}"
    else:
        n = REF_MODES[a.ref]
        print(f"Собираю эталон {a.ref} ({me.cls} {me.spec}, {me.encounter_name})…")
        tops, label = collect_reference(client, me.encounter_id, me.cls, me.spec, me.difficulty,
                                        top_n=n, duration=me.duration, force=a.refresh)
        if a.ref == "median25":
            label = label.replace("топ-25", "медиана топ-25")
    ref = build_reference(tops, load_overrides(a.meta), label=label, me=me)
    r = compare(me, ref)
    out = a.out or f"compare_{_safe(me.name)}_{_safe(me.encounter_name)}.xlsx"
    out = write_compare_workbook(r, out)
    print(f"Запросов к API: {client.requests_made}, из кэша: {client.cache_hits}")
    _print_result(r, out)


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
    from .excel_raid import write_raid_workbook
    from .raid import run_raid
    if a.demo:
        from .raid_demo import DEMO_URL, FakeRaidClient
        client, url = FakeRaidClient(), DEMO_URL
    else:
        if not a.url:
            raise ValueError("Укажите ссылку на отчёт или --demo")
        client, url = _client(), a.url
    R = run_raid(client, url, a.fight)
    out = a.out or f"raid_{_safe(R['info']['boss'])}_pull{R['summary']['pull_n']}.xlsx"
    write_raid_workbook(R, out)
    print()
    dps = f"{R['summary']['raid_dps']:,}".replace(",", " ")
    print(f"{R['info']['boss']} ({R['info']['difficulty']}), {'килл' if R['info']['kill'] else 'вайп'} за "
          f"{R['info']['duration']}: {R['info']['size']} игроков, DPS рейда {dps}")
    print("Что проверить:")
    for i in R["issues"][:10]:
        print(f"  {i['player']}: {i['text']}")
    print(f"\nОтчёт Excel: {Path(out).resolve()}")


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


def _print_result(r, out):
    print()
    print(f"Надёжность сравнения: {r.reliability['overall']}")
    print("Топ-5 отличий:")
    for i, f in enumerate(r.top5, 1):
        imp = f"{f.impact:.1%} DPS" if f.impact else "влияние не оценено"
        print(f"  {i}. [{f.section}] {f.title} — {imp}")
    print(f"\nОтчёт Excel: {Path(out).resolve()}")


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
    d.add_argument("--top", type=int, default=25)
    d.add_argument("--meta", default="spell_meta.json")
    d.set_defaults(func=cmd_demo)

    c = sub.add_parser("compare", help="сравнить ваш лог с топом спека")
    c.add_argument("url", help="ссылка на отчёт WCL (можно с #fight=…&source=…)")
    c.add_argument("--fight", help="номер боя в отчёте (по умолчанию — последний килл)")
    c.add_argument("--player", help="имя персонажа (если в ссылке нет source=)")
    c.add_argument("--ref", choices=list(REF_MODES), default="top25", help="эталонная группа")
    c.add_argument("--against", help="сравнить с конкретным логом (ссылка) вместо топа")
    c.add_argument("--against-fight")
    c.add_argument("--against-player")
    c.add_argument("--meta", default="spell_meta.json", help="переопределения категорий способностей")
    c.add_argument("--refresh", action="store_true", help="заново скачать рейтинг лучших логов")
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
    rd.add_argument("--out")
    rd.set_defaults(func=cmd_raid)

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

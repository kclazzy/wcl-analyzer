"""Обновление ссылок на гайды Mythic Trap: wcl_analyzer/data/guides.json.

Запускается в GitHub Actions (workflow «Ссылки на гайды») раз в неделю и вручную. Можно и у себя:
    python tools/update_guides.py            # собрать и записать файл, если что-то изменилось
    python tools/update_guides.py --dry-run  # только показать, что изменилось бы
    python tools/update_guides.py --dump URL # показать разметку вокруг первой ссылки на способность

Что делает:
1. Открывает главную страницу https://www.mythictrap.com/en и берёт все рейды из блока актуальных
   (всё, что до заголовка «Old Raids»), со списком боссов.
2. Для каждого босса открывает три страницы: эпохальную (/mythic), героическую (/heroic) и обычную.
   Со страницы берутся способности: ссылка на Wowhead (spell=ID) и название рядом с ней.
3. Проверяет результат и записывает файл, только если всё выглядит правдоподобно.
   Если страница не открылась — у этого босса остаются прежние данные.
   Если сайт поменял вёрстку и способностей стало подозрительно мало — файл не трогается,
   скрипт завершается с ошибкой (код 2), чтобы это было видно во вкладке Actions.

Только стандартная библиотека Python: в Actions ничего не нужно ставить.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "wcl_analyzer" / "data" / "guides.json"
SITE = "https://www.mythictrap.com"
BASE = SITE + "/en/"
PAGES = {"mythic": "/mythic", "heroic": "/heroic", "normal": ""}
UA = "WCL-Analyzer guides updater (+https://github.com/kclazzy/wcl-analyzer)"
DELAY_S = 1.0          # пауза между запросами — не нагружаем сайт
MIN_SHARE = 0.6        # у известного рейда способностей должно остаться не меньше 60% от прежнего
MIN_BOSS_ABILITIES = 3  # у босса с гайдом на эпохальной или героической странице хотя бы 3 способности
OLD_MARKER = re.compile(r">\s*Old Raids\s*<", re.I)


# ---------- загрузка ----------

def fetch(url: str, tries: int = 3) -> str | None:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            err = str(e)
        time.sleep(3 * (i + 1))
    print(f"  ! не открылась {url}: {err}")
    return None


# ---------- разбор ----------

def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


BOSS_HREF = re.compile(r"""href=["'](?:https?://(?:www\.)?mythictrap\.com)?/en/([\w-]+)/([\w-]+)/?["'][^>]*>(.*?)</a>""",
                       re.I | re.S)


def parse_raids(page: str) -> list[dict]:
    """Актуальные рейды с главной: [{slug, name, bosses: [{slug, name}]}] в порядке на странице."""
    m = OLD_MARKER.search(page)
    current = page[:m.start()] if m else page
    raids: dict[str, dict] = {}
    for mm in BOSS_HREF.finditer(current):
        raid, boss, inner = mm.group(1), mm.group(2), _text(mm.group(3))
        if boss in PAGES or boss in ("healer", "dps", "tank"):
            continue
        r = raids.get(raid)
        if r is None:
            r = raids[raid] = {"slug": raid, "name": _heading_before(current, mm.start()) or _title(raid),
                               "bosses": []}
        if inner and all(b["slug"] != boss for b in r["bosses"]):
            r["bosses"].append({"slug": boss, "name": inner})
    return [r for r in raids.values() if r["bosses"]]


def _heading_before(page: str, pos: int) -> str:
    heads = list(re.finditer(r"<h[1-6][^>]*>(.*?)</h[1-6]>", page[:pos], re.I | re.S))
    return _text(heads[-1].group(1)) if heads else ""


def _title(slug: str) -> str:
    small = {"of", "the", "and", "in", "on"}
    words = slug.split("-")
    return " ".join(w if i and w in small else w.capitalize() for i, w in enumerate(words))


SPELL_A = re.compile(r"""<a\b[^>]*href=["']https?://(?:\w+\.)?wowhead\.com/(?:[a-z]{2}/)?spell=(\d+)[^"']*["'][^>]*>(.*?)</a>""",
                     re.I | re.S)
TEXT_AFTER = re.compile(r"(?:\s|<[^>]+>)*([^<]+)")


def _good_name(s: str) -> bool:
    return 2 <= len(s) <= 60 and bool(re.search(r"[A-Za-z]", s)) and not s.lower().startswith(("http", "spell="))


def parse_abilities(page: str) -> list[dict]:
    """Способности со страницы босса: [{name, id}] без повторов, в порядке на странице.
    Название — текст ссылки на Wowhead, а если внутри только иконка — первый текст сразу после ссылки."""
    out, seen = [], set()
    for m in SPELL_A.finditer(page):
        sid = int(m.group(1))
        if sid in seen:
            continue
        name = _text(m.group(2))
        if not _good_name(name):
            after = TEXT_AFTER.match(page, m.end())
            name = html.unescape(after.group(1)).strip() if after else ""
        if _good_name(name):
            seen.add(sid)
            out.append({"name": name, "id": sid})
    return out


# ---------- сборка ----------

def load_old() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"raids": []}


def collect(old: dict, only: set | None = None) -> tuple[list[dict], list[str]]:
    main = fetch(BASE.rstrip("/"))
    if not main:
        raise SystemExit("Главная страница Mythic Trap не открылась — файл не меняем.")
    raids = parse_raids(main)
    if not raids:
        raise SystemExit("На главной не нашлось ни одного рейда — похоже, сайт поменял вёрстку. Файл не меняем.")
    if only:
        raids = [r for r in raids if r["slug"] in only]
    for r in raids:  # название рейда, уже известное по файлу, не меняем
        r["name"] = next((o["name"] for o in old.get("raids", []) if o["slug"] == r["slug"]), r["name"])
    old_boss = {(r["slug"], b["slug"]): b for r in old.get("raids", []) for b in r.get("bosses", [])}
    notes = []
    for r in raids:
        print(f"{r['name']} ({r['slug']}): боссов {len(r['bosses'])}")
        for b in r["bosses"]:
            prev = (old_boss.get((r["slug"], b["slug"])) or {}).get("abilities", {})
            ab = {}
            for diff, suffix in PAGES.items():
                time.sleep(DELAY_S)
                page = fetch(f"{BASE}{r['slug']}/{b['slug']}{suffix}")
                got = parse_abilities(page) if page else None
                if got is None or (not got and prev.get(diff)):
                    if prev.get(diff):
                        notes.append(f"{b['name']} ({diff}): страница не разобралась, оставлены прежние данные")
                    got = prev.get(diff, [])
                # способности без id (их нет на Wowhead) переносим из прежнего файла
                ids = {a["id"] for a in got}
                got = got + [a for a in prev.get(diff, []) if a.get("id") is None
                             and a["name"] not in {x["name"] for x in got} and a["id"] not in ids]
                # у того же id сменилось название (сайт поправил или опечатался) — прежнее помним в aka,
                # чтобы поиск по названию находил оба варианта
                was = {x["id"]: x for x in prev.get(diff, []) if x.get("id")}
                for x in got:
                    o = was.get(x["id"])
                    if o:
                        aka = set(o.get("aka", [])) | {o["name"]}
                        aka.discard(x["name"])
                        if aka:
                            x["aka"] = sorted(aka)
                ab[diff] = got
            b["abilities"] = ab
            print(f"  {b['name']}: " + ", ".join(f"{d} {len(v)}" for d, v in ab.items()))
        r["bosses"] = [b for b in r["bosses"] if any(b["abilities"].values())]
    return [r for r in raids if r["bosses"]], notes


def check(new: list[dict], old: dict) -> list[str]:
    """Проблемы, из-за которых файл нельзя записывать."""
    bad = []
    if not new:
        return ["не собрано ни одного рейда с гайдами"]
    count = lambda r: sum(len(v) for b in r["bosses"] for v in b["abilities"].values())  # noqa: E731
    new_by = {r["slug"]: r for r in new}
    for r in old.get("raids", []):
        n = new_by.get(r["slug"])
        if n is None:
            continue  # рейд ушёл в «Old Raids» — это нормально, его данные сохраняются
        if count(n) < MIN_SHARE * count(r):
            bad.append(f"{r['name']}: способностей {count(n)} вместо {count(r)}")
    for r in new:
        for b in r["bosses"]:
            top = max(len(b["abilities"].get("mythic", [])), len(b["abilities"].get("heroic", [])))
            if 0 < top < MIN_BOSS_ABILITIES:
                bad.append(f"{r['name']} / {b['name']}: всего {top} способности")
    return bad


def merge(new: list[dict], old: dict) -> list[dict]:
    """Свежие рейды сверху, рейды из прежнего файла, которых уже нет в актуальных, — сохраняем ниже."""
    have = {r["slug"] for r in new}
    return new + [r for r in old.get("raids", []) if r["slug"] not in have]


def summary(new: list[dict], old: dict) -> list[str]:
    ob = {(r["slug"], b["slug"]): b for r in old.get("raids", []) for b in r.get("bosses", [])}
    oraids = {r["slug"] for r in old.get("raids", [])}
    lines = []
    for r in new:
        if r["slug"] not in oraids:
            lines.append(f"Новый рейд: {r['name']} — боссов {len(r['bosses'])}")
        for b in r["bosses"]:
            o = ob.get((r["slug"], b["slug"]))
            if o is None:
                if r["slug"] in oraids:
                    lines.append(f"Новый босс: {r['name']} / {b['name']}")
                continue
            for diff in PAGES:
                a = {x["name"] for x in b["abilities"].get(diff, [])}
                p = {x["name"] for x in o["abilities"].get(diff, [])}
                if a != p:
                    plus, minus = sorted(a - p), sorted(p - a)
                    lines.append(f"{b['name']} ({diff}): " + "; ".join(
                        x for x in (("+ " + ", ".join(plus)) if plus else "", ("− " + ", ".join(minus)) if minus else "") if x))
    return lines


def _counts(raids: list[dict]) -> list[str]:
    return [f"{r['name']} / {b['name']}: " + ", ".join(f"{d} {len(v)}" for d, v in b["abilities"].items())
            for r in raids for b in r["bosses"]]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="ничего не записывать")
    ap.add_argument("--raid", action="append", help="только этот рейд (slug), можно несколько")
    ap.add_argument("--dump", metavar="URL", help="показать разметку вокруг первой ссылки на Wowhead и выйти")
    ap.add_argument("--find", metavar="TEXT", help="с --dump: показать разметку вокруг этого текста")
    a = ap.parse_args(argv)

    if a.dump:
        page = fetch(a.dump) or ""
        parts = [f"Длина страницы {len(page)}"]
        i = page.find("wowhead.com")
        parts.append("== вокруг первой ссылки на Wowhead ==\n" + (page[max(0, i - 600):i + 900] if i >= 0 else page[:1500]))
        if a.find:
            hits = [m.start() for m in re.finditer(re.escape(a.find), page)][:3]
            parts.append(f"== «{a.find}»: найдено {len(hits)} ==")
            parts += [page[max(0, h - 900):h + 400] for h in hits]
        parts.append("== скрипты ==\n" + "\n".join(re.findall(r"<script[^>]*src=[\"']([^\"']+)", page)[:40]))
        for p_ in parts:
            print(p_)
            if os.environ.get("GITHUB_ACTIONS"):
                print("::notice title=dump::" + p_[:6000].replace("%", "%25").replace("\r", "").replace("\n", "%0A"))
        return 0

    old = load_old()
    try:
        raids, notes = collect(old, set(a.raid) if a.raid else None)
    except SystemExit as e:
        print(e)
        _step_summary(["### Ссылки на гайды: файл не обновлён", f"- {e}"])
        return 2
    problems = check(raids, old)
    for n in notes:
        print("Примечание:", n)
    if problems:
        print("\nФайл НЕ обновлён — результат выглядит неправдоподобно (возможно, сайт поменял вёрстку):")
        for p in problems:
            print("  -", p)
        _step_summary(["### Ссылки на гайды: файл не обновлён", *[f"- {p}" for p in problems]], _counts(raids))
        return 2

    merged = merge(raids, old)
    changes = summary(raids, old)
    same = json.dumps(merged, sort_keys=True) == json.dumps(old.get("raids", []), sort_keys=True)
    total = sum(len(v) for r in merged for b in r["bosses"] for v in b["abilities"].values())
    print(f"\nРейдов {len(merged)}, боссов {sum(len(r['bosses']) for r in merged)}, записей способностей {total}.")
    if same:
        print("Изменений нет.")
        _step_summary(["### Ссылки на гайды: изменений нет"], _counts(raids))
        return 0
    print("Изменения:\n  " + "\n  ".join(changes or ["порядок или названия"]))
    _step_summary(["### Ссылки на гайды: " + ("найдены изменения (проверка)" if a.dry_run else "обновлены"),
                   *[f"- {c}" for c in changes]], _counts(raids))
    if a.dry_run:
        print("(--dry-run: файл не записан)")
        return 0
    data = {
        "_about": old.get("_about") or "Гайды Mythic Trap по боссам. Файл обновляет tools/update_guides.py.",
        "version": 2, "site": "Mythic Trap", "base": BASE, "pages": PAGES,
        "updated": dt.date.today().isoformat(), "raids": merged,
    }
    FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Записано: {FILE.relative_to(ROOT)}")
    return 0


def _step_summary(lines: list[str], counts: list[str] | None = None) -> None:
    """Итог запуска: таблица на странице запуска в Actions и короткое сообщение (annotation) над ней."""
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n".join(lines + [""] + [f"- {c}" for c in (counts or [])]) + "\n")
    if os.environ.get("GITHUB_ACTIONS"):
        msg = "\n".join(lines[1:] + (counts or []))[:30000] or "—"
        kind = "error" if "не обновлён" in lines[0] else "notice"
        title = lines[0].lstrip("# ")
        print(f"::{kind} title={title}::" + msg.replace("%", "%25").replace("\r", "").replace("\n", "%0A"))


if __name__ == "__main__":
    sys.exit(main())

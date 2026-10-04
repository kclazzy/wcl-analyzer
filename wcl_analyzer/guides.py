"""Ссылки на гайды по способностям боссов (Mythic Trap). Данные лежат в программе (data/guides.json),
у пользователя ничего не скачивается. Файл обновляет tools/update_guides.py по расписанию в GitHub Actions,
новые ссылки приходят вместе с обновлением программы.

Способность из лога сопоставляется по id заклинания, а если id другой (у механики бывает несколько
заклинаний: каст, урон, дебафф) — по английскому названию, сначала у босса этого боя.
Нет совпадения — ссылки нет, остаётся просто название."""
from __future__ import annotations

import json
import re
from pathlib import Path

FILE = Path(__file__).parent / "data" / "guides.json"
DIFF_PAGE = {5: "mythic", 4: "heroic"}   # остальные сложности — общая страница босса
_DATA: dict | None = None


def _data() -> dict:
    global _DATA
    if _DATA is None:
        try:
            _DATA = json.loads(FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _DATA = {"raids": []}
    return _DATA


def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", " ", (s or "").lower()).strip()


def bosses() -> list[tuple[dict, dict]]:
    """[(рейд, босс)] из всех рейдов файла."""
    return [(r, b) for r in _data().get("raids", []) for b in r.get("bosses", [])]


def _url(raid: dict, boss: dict, page: str) -> str:
    d = _data()
    return d["base"] + raid["slug"] + "/" + boss["slug"] + d["pages"].get(page, "")


def _find(rb: list, spell_id, nn: str, want: str) -> str | None:
    for raid, b in rb:
        for diff in (want, "mythic", "heroic", "normal"):  # сначала страница своей сложности
            for a in b.get("abilities", {}).get(diff, []):
                if (spell_id and a.get("id") == int(spell_id)) or (nn and _norm(a["name"]) == nn):
                    return _url(raid, b, diff)
    return None


def link(spell_id: int | None, name: str | None, difficulty: int | None = None,
         boss: str | None = None) -> str | None:
    """Ссылка на страницу гайда босса нужной сложности, где описана эта способность; иначе None.
    boss — название босса боя: по названию способности ищем сначала у него (в разных рейдах бывают тёзки)."""
    want = DIFF_PAGE.get(int(difficulty or 0), "normal")
    nn = _norm(name or "")
    rb = bosses()
    if spell_id:  # id заклинания уникален — ищем по всем рейдам
        url = _find(rb, spell_id, "", want)
        if url:
            return url
    if not nn:
        return None
    if boss:
        own = [x for x in rb if _norm(x[1].get("name", "")) == _norm(boss)]
        if own:
            return _find(own, None, nn, want)
    return _find(rb, None, nn, want)


def links_for(names: dict, difficulty: int | None, boss: str | None = None) -> dict:
    """{название в отчёте: ссылка} для способностей из names ({id: название}), у которых есть гайд."""
    out = {}
    for sid, nm in names.items():
        if nm in out:
            continue
        url = link(sid, nm, difficulty, boss)
        if url:
            out[nm] = url
    return out

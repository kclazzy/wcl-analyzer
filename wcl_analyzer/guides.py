"""Ссылки на гайды по способностям боссов (Mythic Trap). Данные лежат в программе (data/guides.json),
ничего не скачивается. Способность из лога сопоставляется по id заклинания, а если id другой
(у механики бывает несколько заклинаний: каст, урон, дебафф) — по английскому названию.
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
            _DATA = {"bosses": []}
    return _DATA


def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", " ", (s or "").lower()).strip()


def link(spell_id: int | None, name: str | None, difficulty: int | None = None) -> str | None:
    """Ссылка на страницу гайда босса нужной сложности, где описана эта способность; иначе None."""
    d = _data()
    want = DIFF_PAGE.get(int(difficulty or 0), "normal")
    nn = _norm(name or "")
    for b in d.get("bosses", []):
        found = None
        for diff in (want, "mythic", "heroic", "normal"):  # сначала страница своей сложности
            for a in b["abilities"].get(diff, []):
                if (spell_id and a.get("id") == int(spell_id)) or (nn and _norm(a["name"]) == nn):
                    found = diff
                    break
            if found:
                break
        if found:
            return d["base"] + b["slug"] + d["pages"].get(found, "")
    return None


def links_for(names: dict, difficulty: int | None) -> dict:
    """{название в отчёте: ссылка} для способностей из names ({id: название}), у которых есть гайд."""
    out = {}
    for sid, nm in names.items():
        if nm in out:
            continue
        url = link(sid, nm, difficulty)
        if url:
            out[nm] = url
    return out

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


def _find(rb: list, spell_id, nn: str, want: str) -> dict | None:
    for raid, b in rb:
        hit = None
        for diff in (want, "mythic", "heroic", "normal"):  # сначала страница своей сложности
            for a in b.get("abilities", {}).get(diff, []):
                if (spell_id and a.get("id") == int(spell_id)) or (nn and nn in {_norm(x) for x in [a["name"], *a.get("aka", [])]}):
                    hit = hit or {"url": a.get("share") or _url(raid, b, diff), "video": a.get("video")}
                    hit["video"] = hit["video"] or a.get("video")  # ролик бывает только на одной из сложностей
                    break
        if hit:
            return hit
    return None


def link(spell_id: int | None, name: str | None, difficulty: int | None = None,
         boss: str | None = None) -> str | None:
    """Ссылка «Share link» на саму способность в гайде (…/boss/heroic?ability=ключ), а если её нет —
    на страницу босса нужной сложности; если способность не найдена — None."""
    g = find(spell_id, name, difficulty, boss)
    return g["url"] if g else None


def find(spell_id: int | None, name: str | None, difficulty: int | None = None,
         boss: str | None = None) -> dict | None:
    """{url: ссылка на способность в гайде, video: адрес ролика механики или None}; не найдена — None.
    В программе хранится только адрес ролика, само видео грузится с Mythic Trap при просмотре.
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
        g = find(sid, nm, difficulty, boss)
        if g:
            out[nm] = g["url"]
    return out

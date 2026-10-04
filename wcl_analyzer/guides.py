"""Ссылки на гайды по способностям боссов (Mythic Trap). Данные лежат в программе (data/guides.json),
у пользователя ничего не скачивается. Файл обновляет tools/update_guides.py по расписанию в GitHub Actions,
новые ссылки приходят вместе с обновлением программы.

Способность из лога сопоставляется по id заклинания, а если id другой (у механики бывает несколько
заклинаний: каст, урон, дебафф) — по названию (английскому или русскому), сначала у босса этого боя.
Нет совпадения — ссылки нет (в разборе тогда ссылка на Wowhead, см. wowhead.py).

Краткое описание: русские название и описание с Wowhead (data/guides.json → spells), тип механики и
совет «что делать» с Mythic Trap — в переводе из data/guides_ru.json (нет перевода — по-английски)."""
from __future__ import annotations

import json
import re
from pathlib import Path

FILE = Path(__file__).parent / "data" / "guides.json"
RU_FILE = Path(__file__).parent / "data" / "guides_ru.json"
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


_RU: dict | None = None


def _ru() -> dict:
    global _RU
    if _RU is None:
        try:
            _RU = json.loads(RU_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _RU = {}
    return _RU


def ru_text(kind: str, text: str | None) -> str | None:
    """Перевод типа механики (kind="type") или совета (kind="todo"); нет перевода — как есть."""
    if not text:
        return None
    return _ru().get(kind, {}).get(text) or text


def spell_ru(spell_id) -> dict:
    """{name, desc} на русском с Wowhead для способности из гайдов (хранится в программе)."""
    return (_data().get("spells") or {}).get(str(spell_id)) or {} if spell_id else {}


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
                if (spell_id and a.get("id") == int(spell_id)) or (nn and nn in {
                        _norm(x) for x in [a["name"], *a.get("aka", []), spell_ru(a.get("id")).get("name") or ""] if x}):
                    if hit is None:
                        sr = spell_ru(a.get("id"))
                        hit = {"url": a.get("share") or _url(raid, b, diff), "video": a.get("video"), "src": "mt",
                               "id": a.get("id"), "name_en": a["name"], "name_ru": sr.get("name"),
                               "desc": sr.get("desc"), "type": ru_text("type", a.get("type")),
                               "todo": ru_text("todo", a.get("todo")), "boss": b.get("name")}
                    for k in ("video", "type", "todo"):  # бывает только на одной из сложностей
                        if not hit.get(k) and a.get(k):
                            hit[k] = a[k] if k == "video" else ru_text(k, a[k])
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
    """{url: ссылка на способность в гайде, video: адрес ролика механики или None, src: "mt",
    name_ru, desc: русское описание (Wowhead), type: тип механики, todo: что делать}; не найдена — None.
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

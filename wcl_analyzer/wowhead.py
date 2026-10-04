"""Краткое описание способности с Wowhead — на русском, официальный игровой текст.

Wowhead отдаёт подсказку заклинания (то, что видно при наведении на ссылку) в виде JSON:
https://nether.wowhead.com/tooltip/spell/ID?dataEnv=1&locale=7 (7 — русский).
Отсюда берутся русское название и описание.

Используется в двух местах:
- tools/update_guides.py — раз в неделю для всех способностей из гайдов Mythic Trap, результат
  хранится в программе (data/guides.json), при разборе ничего не скачивается;
- при разборе — только для способностей, которых нет в гайдах: несколько коротких запросов,
  ответы запоминаются (файл wowhead_ru.json в папке данных; публичный сервер — только в памяти).
"""
from __future__ import annotations

import html
import json
import re
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

TOOLTIP = "https://nether.wowhead.com/tooltip/spell/{id}?dataEnv=1&locale=7"
PAGE = "https://www.wowhead.com/ru/spell={id}"
UA = "WCL-Analyzer (+https://github.com/kclazzy/wcl-analyzer)"
MAX_DESC = 280

_MEM: dict = {}
_LOCK = threading.Lock()


def page_url(spell_id) -> str:
    return PAGE.format(id=int(spell_id))


def _text(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", " ", fragment, flags=re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def shorten(text: str, limit: int = MAX_DESC) -> str:
    """Не длиннее limit символов, по границе предложения (или слова)."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= limit // 3:
        return cut[:end + 1]
    return cut[:cut.rfind(" ")].rstrip(",;:—- ") + "…"


def parse_tooltip(data: dict) -> dict | None:
    """{name, desc} из ответа Wowhead. Описание — последняя таблица подсказки (первая — название и время)."""
    if not isinstance(data, dict) or not data.get("name"):
        return None
    tables = re.findall(r"<table>(.*?)</table>", data.get("tooltip") or "", re.S)
    desc = _text(tables[-1]) if len(tables) >= 2 else ""
    return {"name": html.unescape(data["name"]).strip(), "desc": shorten(desc)}


def fetch(spell_id, timeout: float = 8) -> dict | None:
    """Подсказка с Wowhead (без кэша). None — не нашлось или нет связи."""
    try:
        req = urllib.request.Request(TOOLTIP.format(id=int(spell_id)), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return parse_tooltip(json.loads(r.read().decode("utf-8", "replace")))
    except Exception:  # noqa: BLE001 — описание необязательно
        return None


def _cache_file():
    try:
        from .config import data_dir
        return data_dir() / "wowhead_ru.json"
    except Exception:  # noqa: BLE001
        return None


def _load_disk() -> dict:
    f = _cache_file()
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f and f.exists() else {}
    except (OSError, ValueError):
        return {}


def lookup(spell_ids, save: bool = True, limit: int = 15) -> dict:
    """{id: {name, desc}} для способностей, которых нет в гайдах. Уже известные — из памяти и файла,
    остальные (не больше limit) — параллельными запросами к Wowhead. save=False — ничего не пишем на диск."""
    ids = [int(i) for i in dict.fromkeys(spell_ids) if i]
    with _LOCK:
        if save and not _MEM.get("_disk"):
            _MEM.update(_load_disk())
            _MEM["_disk"] = True
        out = {i: _MEM[str(i)] for i in ids if str(i) in _MEM}
    todo = [i for i in ids if i not in out][:limit]
    if todo:
        with ThreadPoolExecutor(max_workers=min(6, len(todo))) as ex:
            for i, info in zip(todo, ex.map(fetch, todo)):
                if info:
                    out[i] = info
        with _LOCK:
            for i in todo:
                if i in out:
                    _MEM[str(i)] = out[i]
            if save:
                f = _cache_file()
                if f:
                    try:
                        f.parent.mkdir(parents=True, exist_ok=True)
                        f.write_text(json.dumps({k: v for k, v in _MEM.items() if k != "_disk"},
                                                ensure_ascii=False), encoding="utf-8")
                    except OSError:
                        pass
    return out

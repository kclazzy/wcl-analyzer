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
VERSION = 2   # способ обработки описаний; поменялся — сборщик гайдов перекачивает описания заново

SAVE = {"enabled": True}   # публичный сервер ничего не пишет на диск (web.serve выключает)
_MEM: dict = {}
_LOCK = threading.Lock()


def page_url(spell_id) -> str:
    return PAGE.format(id=int(spell_id))


def _text(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", " ", fragment, flags=re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


_ABBR = {"ед", "сек", "мин", "ч", "м", "т", "д", "др", "см", "прим", "макс", "мин", "пр"}


def clean(text: str) -> str:
    """Без служебных формул Wowhead («([514.5% of Spell Power])») и лишних пробелов перед знаками."""
    text = re.sub(r"\s*\(?\[[^\]]*\]\)?", "", text or "")
    text = re.sub(r"\s+([.,;:!?)])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r'"\s*([^"]+?)\s*"', r"«\1»", text)  # " Вязкая киста " → «Вязкая киста»
    return re.sub(r"\s{2,}", " ", text).strip()


def shorten(text: str, limit: int = MAX_DESC) -> str:
    """Не длиннее limit символов, по границе предложения (или слова)."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # конец предложения — точка после слова, которое не сокращение («ед.», «сек.», «мин.», «м.»)
    ends = [m.end(1) for m in re.finditer(r"(\w+[.!?])\s+(?=[A-ZА-ЯЁ«])", cut)
            if m.group(1)[:-1].lower() not in _ABBR]
    end = ends[-1] if ends else -1
    if end >= limit // 3:
        return cut[:end]
    return cut[:cut.rfind(" ")].rstrip(",;:—- ") + "…"


def parse_tooltip(data: dict) -> dict | None:
    """{name, desc} из ответа Wowhead. Описание — последняя таблица подсказки (первая — название и время)."""
    if not isinstance(data, dict) or not data.get("name"):
        return None
    tables = re.findall(r"<table>(.*?)</table>", data.get("tooltip") or "", re.S)
    desc = _text(tables[-1]) if len(tables) >= 2 else ""
    return {"name": html.unescape(data["name"]).strip(), "desc": shorten(clean(desc))}


def _get_json(url: str, timeout: float):
    """JSON по адресу. Через requests с сертификатами certifi — как запросы к Warcraft Logs: в Android-приложении
    у стандартного urllib нет списка корневых сертификатов, и HTTPS к Wowhead там молча не работал."""
    try:
        import requests
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except ImportError:
        pass
    import ssl
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch(spell_id, timeout: float = 8) -> dict | None:
    """Подсказка с Wowhead (без кэша). None — не нашлось или нет связи."""
    try:
        return parse_tooltip(_get_json(TOOLTIP.format(id=int(spell_id)), timeout))
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
        d = json.loads(f.read_text(encoding="utf-8")) if f and f.exists() else {}
        return d if d.pop("_ver", None) == VERSION else {}   # старая обработка описаний — забываем
    except (OSError, ValueError):
        return {}


def lookup(spell_ids, save: bool | None = None, limit: int = 15) -> dict:
    """{id: {name, desc}} для способностей, которых нет в гайдах. Уже известные — из памяти и файла,
    остальные (не больше limit) — параллельными запросами к Wowhead. save=False — ничего не пишем на диск."""
    save = SAVE["enabled"] if save is None else save
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
                        f.write_text(json.dumps({"_ver": VERSION, **{k: v for k, v in _MEM.items() if k != "_disk"}},
                                                ensure_ascii=False), encoding="utf-8")
                    except OSError:
                        pass
    return out

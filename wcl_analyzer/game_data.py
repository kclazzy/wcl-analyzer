"""Игровые данные, которые меняются с патчами: рейдовые кулдауны спеков (откат, сила, как узнать
нажатие) и способности жажды крови.

Одна таблица на всю программу — wcl_analyzer/data/game_data.json. При первом обращении программа
пробует взять свежую версию файла из репозитория (раз в сутки, ответ хранится на устройстве), иначе —
копию из сборки. Так к новому патчу достаточно поправить файл в репозитории, без пересборки.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

REMOTE_URL = os.environ.get(
    "WCL_GAME_DATA_URL",
    "https://raw.githubusercontent.com/kclazzy/wcl-analyzer/main/wcl_analyzer/data/game_data.json")
# Запасной адрес — то же через jsDelivr (если raw.githubusercontent.com недоступен)
REMOTE_FALLBACK = os.environ.get(
    "WCL_GAME_DATA_URL2", "https://cdn.jsdelivr.net/gh/kclazzy/wcl-analyzer@main/wcl_analyzer/data/game_data.json")
BUNDLED = Path(__file__).parent / "data" / "game_data.json"
MAX_AGE_S = 86400

_LOCK = threading.Lock()
_DATA: dict | None = None
SAVE = {"enabled": True}  # публичный сервер выключает запись на диск


def _valid(d) -> bool:
    return (isinstance(d, dict) and isinstance(d.get("raid_cds"), list) and d["raid_cds"]
            and all(isinstance(c, dict) and "id" in c and "cd" in c for c in d["raid_cds"]))


def _load() -> dict:
    bundled = json.loads(BUNDLED.read_text(encoding="utf-8"))
    if os.environ.get("WCL_GAME_DATA") == "bundled":
        return bundled
    try:
        from .config import data_dir
        local = data_dir() / "game_data.json"
    except Exception:  # noqa: BLE001
        local = None
    try:
        if local and local.exists() and time.time() - local.stat().st_mtime < MAX_AGE_S:
            d = json.loads(local.read_text(encoding="utf-8"))
            if _valid(d):
                return d
    except (OSError, ValueError):
        pass
    for url in (REMOTE_URL, REMOTE_FALLBACK):
        try:
            import requests
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            d = r.json()
        except Exception:  # noqa: BLE001
            continue
        if _valid(d):
            if local and SAVE["enabled"]:
                try:
                    local.parent.mkdir(parents=True, exist_ok=True)
                    local.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
            return d
    return bundled  # нет сети или файла в репозитории: копия из сборки


def data() -> dict:
    global _DATA
    if _DATA is None:
        with _LOCK:
            if _DATA is None:
                _DATA = _load()
    return _DATA


def raid_cds() -> list[dict]:
    return data()["raid_cds"]


def cd_ids() -> set[int]:
    return {int(c["id"]) for c in raid_cds()}


def cooldown(sid: int, default: float | None = None) -> float | None:
    return next((float(c["cd"]) for c in raid_cds() if int(c["id"]) == int(sid)), default)


def class_of(sid: int) -> str:
    """Класс по рейдовому кулдауну (из таблицы игровых данных); неизвестный — пустая строка."""
    for src in (raid_cds(), _bundled_cds()):
        for c in src:
            if int(c["id"]) == int(sid) and c.get("class"):
                return str(c["class"])
    return ""


def scope(sid: int) -> str:
    """raid — действует сразу на рейд; self — усиливает исцеление самого лекаря. Неизвестные — raid."""
    for src in (raid_cds(), _bundled_cds()):  # в старой копии таблицы поля scope ещё нет — берём из сборки
        for c in src:
            if int(c["id"]) == int(sid) and c.get("scope"):
                return str(c["scope"])
    return "raid"


_BUNDLED_CDS: list | None = None


def _bundled_cds() -> list[dict]:
    global _BUNDLED_CDS
    if _BUNDLED_CDS is None:
        try:
            _BUNDLED_CDS = json.loads(BUNDLED.read_text(encoding="utf-8")).get("raid_cds") or []
        except (OSError, ValueError):
            _BUNDLED_CDS = []
    return _BUNDLED_CDS


def power(sid: int, default: int = 2) -> int:
    return next((int(c.get("power", default)) for c in raid_cds() if int(c["id"]) == int(sid)), default)


_RE_CACHE: dict = {}


def cd_name_re() -> re.Pattern:
    key = id(data())
    if key in _RE_CACHE:
        return _RE_CACHE[key]
    names = set()
    for c in raid_cds():
        names |= {c.get("en", ""), c.get("name", "")} | set(c.get("aliases") or [])
    names = sorted((n.lower() for n in names if n), key=len, reverse=True)
    _RE_CACHE[key] = re.compile("|".join(re.escape(n) for n in names), re.I)
    return _RE_CACHE[key]


def lust_ids() -> set[int]:
    return {int(x) for x in data().get("lust_ids") or []}


class LazyIds:
    """Множество id, которое берётся из таблицы при первой проверке (а не при импорте)."""

    def __init__(self, getter):
        self._get = getter

    def __contains__(self, x) -> bool:
        return x in self._get()

    def __iter__(self):
        return iter(self._get())

    def __len__(self) -> int:
        return len(self._get())


class LazyRe:
    def __init__(self, getter):
        self._get = getter

    def search(self, s):
        return self._get().search(s)

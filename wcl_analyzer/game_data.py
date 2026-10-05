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


def _local_path():
    try:
        from .config import data_dir
        return data_dir() / "game_data.json"
    except Exception:  # noqa: BLE001
        return None


def _fetch_remote() -> dict | None:
    for url in (REMOTE_URL, REMOTE_FALLBACK):
        try:
            import requests
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            d = r.json()
        except Exception:  # noqa: BLE001
            continue
        if _valid(d):
            local = _local_path()
            if local and SAVE["enabled"]:
                try:
                    local.parent.mkdir(parents=True, exist_ok=True)
                    local.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
            return d
    return None


def _load() -> tuple[dict, bool]:
    """(таблица, свежая ли). Сразу — сохранённая копия (даже вчерашняя: она новее копии из сборки) или из сборки;
    свежая из репозитория скачивается в фоне — разбор не ждёт сеть."""
    bundled = json.loads(BUNDLED.read_text(encoding="utf-8"))
    if os.environ.get("WCL_GAME_DATA") == "bundled":
        return bundled, True
    local = _local_path()
    try:
        if local and local.exists():
            d = json.loads(local.read_text(encoding="utf-8"))
            if _valid(d) and int(d.get("version") or 0) >= int(bundled.get("version") or 0):
                return d, time.time() - local.stat().st_mtime < MAX_AGE_S
    except (OSError, ValueError):
        pass
    return bundled, False


_LOADED_AT = 0.0
_REFRESHING = threading.Event()


def _refresh_bg() -> None:
    """Свежая таблица из репозитория — в фоне; пришла — подменяется целиком (кэши по id(data()) обновятся сами)."""
    global _DATA, _LOADED_AT
    if _REFRESHING.is_set():
        return
    _REFRESHING.set()

    def run():
        global _DATA, _LOADED_AT
        try:
            d = _fetch_remote()
            if d is not None:
                _DATA = d
            _LOADED_AT = time.time()
        finally:
            _REFRESHING.clear()
    threading.Thread(target=run, daemon=True).start()


def data() -> dict:
    global _DATA, _LOADED_AT
    if _DATA is None:
        with _LOCK:
            if _DATA is None:
                d, fresh = _load()
                _DATA = d
                _LOADED_AT = time.time() if fresh else 0.0
    if time.time() - _LOADED_AT > MAX_AGE_S and os.environ.get("WCL_GAME_DATA") != "bundled":
        _LOADED_AT = time.time()   # раз в сутки, даже если программа не закрывается
        _refresh_bg()
    return _DATA


def raid_cds() -> list[dict]:
    return data()["raid_cds"]


_MEMO: dict = {}


def _memo(name: str, build):
    """Значение, посчитанное один раз на загруженную таблицу (раньше — заново на каждое событие лога)."""
    key = (name, id(data()))
    if key not in _MEMO:
        if len(_MEMO) > 64:
            _MEMO.clear()
        _MEMO[key] = build()
    return _MEMO[key]


def _index() -> dict[int, dict]:
    """id → {cd, class, scope, power}: из таблицы, недостающие class/scope — из копии в сборке."""
    def build():
        out: dict[int, dict] = {}
        for src in (raid_cds(), _bundled_cds()):
            for c in src:
                e = out.setdefault(int(c["id"]), {})
                for k in ("cd", "class", "scope", "power"):
                    if k not in e and c.get(k) not in (None, ""):
                        if k in ("cd", "power") and src is not raid_cds():
                            continue   # откат и сила — только из действующей таблицы, как раньше
                        e[k] = c[k]
        return out
    return _memo("index", build)


def cd_ids() -> set[int]:
    return _memo("cd_ids", lambda: frozenset(int(c["id"]) for c in raid_cds()))


def cooldown(sid: int, default: float | None = None) -> float | None:
    v = _index().get(int(sid), {}).get("cd")
    return float(v) if v is not None else default


def class_of(sid: int) -> str:
    """Класс по рейдовому кулдауну (из таблицы игровых данных); неизвестный — пустая строка."""
    return str(_index().get(int(sid), {}).get("class") or "")


def scope(sid: int) -> str:
    """raid — действует сразу на рейд; self — усиливает исцеление самого лекаря. Неизвестные — raid."""
    return str(_index().get(int(sid), {}).get("scope") or "raid")


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
    v = _index().get(int(sid), {})
    return int(v["power"]) if "power" in v else default


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


def _key(s) -> str:
    return "".join(ch for ch in str(s or "") if ch.isalnum()).lower()


def name_known(name: str, cls: str = "", spec: str = "") -> bool:
    """Способность — рейдовый кулдаун из таблицы по НАЗВАНИЮ (когда id в логе другой): только полное
    совпадение названия (не часть слова: «Сумрак» ≠ «Мрак») и только у того класса и спека, чей это кулдаун —
    «Перерождение» шамана-энха или «Возрождение» друида (боевое воскрешение) рейдовыми сейвами не считаются."""
    n = (name or "").strip().lower()
    if not n:
        return False

    def build():
        idx: dict = {}
        for c in raid_cds():
            for x in {x.lower() for x in [c.get("en", ""), c.get("name", ""), *(c.get("aliases") or [])] if x}:
                idx.setdefault(x, []).append(c)
        return idx
    for c in _memo("names", build).get(n, []):
        if cls and c.get("class") and _key(c["class"]) != _key(cls):
            continue
        if spec and c.get("spec") and _key(c["spec"]) != _key(spec):
            continue
        return True
    return False


_BUNDLED_SECTIONS: dict = {}


def _section(name: str) -> list:
    """Раздел таблицы; в старой копии с устройства его может не быть — берём из сборки (читается один раз)."""
    d = data().get(name)
    if d:
        return d
    if name not in _BUNDLED_SECTIONS:
        try:
            _BUNDLED_SECTIONS[name] = json.loads(BUNDLED.read_text(encoding="utf-8")).get(name) or []
        except (OSError, ValueError):
            _BUNDLED_SECTIONS[name] = []
    return _BUNDLED_SECTIONS[name]


def dps_cds() -> list[dict]:
    """Крупные боевые кулдауны DPS и внешние усиления урона (Придание сил) — вкладка «Нанесение урона»."""
    return _section("dps_cds")


_IDS_CACHE: dict = {}


def dps_cd_ids() -> set[int]:
    src = dps_cds()
    if _IDS_CACHE.get("src") is not src:
        _IDS_CACHE["src"], _IDS_CACHE["ids"] = src, {int(c["id"]) for c in src}
    return _IDS_CACHE["ids"]


def tank_cds() -> list[dict]:
    """Защитные кулдауны танков и внешние сейвы на танка — вкладка «Танки»."""
    return _section("tank_cds")


def tank_cd_ids() -> set[int]:
    return _memo("tank_ids", lambda: frozenset(int(c["id"]) for c in tank_cds()))


def amp_windows() -> list[dict]:
    """Механики босса, под которые жмут бурсты (уязвимость, снятый щит…): id и/или название."""
    return _section("amp_windows")


def lust_ids() -> set[int]:
    return _memo("lust_ids", lambda: frozenset(int(x) for x in _section("lust_ids")))


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

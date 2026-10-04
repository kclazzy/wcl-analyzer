"""Загрузчик обновлений кода — часть «оболочки» (.exe / APK), сам не обновляется.

Программа умеет скачивать новую версию своего кода (пакет wcl_analyzer) в папку данных и запускать
её вместо встроенной — без переустановки. Этот модуль при запуске:
  1. смотрит, есть ли скачанный код и подходит ли он к этой оболочке (SHELL_ID — отпечаток
     версии Python, библиотек и настроек сборки; если оболочка другая, скачанный код не берётся);
  2. подключает его раньше встроенного (свой поисковик модулей — иначе PyInstaller отдаёт встроенный);
  3. если скачанный код не импортируется — откатывается на встроенный и помечает обновление сломанным.

«Ждёт подтверждения → подтверждён»: при первом запуске новой сборки в её папке появляется метка
.pending; как только веб-сервер этой сборки запустился, код программы (update.confirm_running_build)
ставит метку .confirmed. Если при следующем запуске метка .pending есть, а .confirmed нет — прошлый
запуск упал, не дойдя до сервера: сборка помечается сломанной (failed.json) и больше не запускается,
программа работает на встроенном коде. Более новая сборка скачается и попробуется как обычно.
"""
from __future__ import annotations

import importlib.machinery
import json
import os
import sys
import time
from pathlib import Path

SHELL_ID = "dev"   # сборка на GitHub подставляет сюда отпечаток оболочки
PKG = "wcl_analyzer"
PENDING, CONFIRMED, FAILED = ".pending", ".confirmed", "failed.json"
# Метка .pending моложе этого — скорее всего, программу просто запустили второй раз подряд,
# а первый запуск ещё не успел подтвердить сборку: сломанной её не считаем
PENDING_GRACE_S = 20


def data_dir() -> Path:
    """Та же папка данных, что у программы (config.data_dir), но без импорта самой программы."""
    if os.environ.get("WCL_DATA_DIR"):
        return Path(os.environ["WCL_DATA_DIR"])
    if os.environ.get("WCL_APP") == "android" or "ANDROID_PRIVATE" in os.environ or "ANDROID_ARGUMENT" in os.environ:
        return Path(os.environ.get("ANDROID_PRIVATE") or os.getcwd()) / "wcl-data"
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")) / "WCL Analyzer"
    return Path(".")


def code_root() -> Path:
    return data_dir() / "code"


def failed_builds(root: Path | None = None) -> set[str]:
    """Номера сборок, которые на этом устройстве не запустились (их больше не пробуем)."""
    try:
        d = json.loads(((root or code_root()) / FAILED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(d, dict):
        return set()
    out = {str(b) for b in d.get("builds") or [] if b is not None}
    if d.get("build") is not None:  # прежний формат: один номер
        out.add(str(d["build"]))
    return out


def mark_failed(build, why: str = "", root: Path | None = None) -> None:
    """Сборка сломана: в список failed.json, и current.json больше на неё не указывает."""
    root = root or code_root()
    builds = sorted(failed_builds(root) | {str(build)}, key=lambda b: (len(b), b))
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / FAILED).write_text(json.dumps({"build": str(build), "builds": builds, "why": str(why)[:300],
                                               "time": int(time.time())}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    cur = root / "current.json"
    try:
        if str(json.loads(cur.read_text(encoding="utf-8")).get("build")) == str(build):
            cur.unlink()
    except (OSError, ValueError, AttributeError):
        pass


class _Finder:
    """Ищет wcl_analyzer и его модули сначала в скачанном коде."""

    def __init__(self, root: str):
        self.root = root

    def find_spec(self, name, path=None, target=None):
        if name != PKG and not name.startswith(PKG + "."):
            return None
        if name == PKG:
            return importlib.machinery.PathFinder.find_spec(name, [self.root])
        parent = sys.modules.get(name.rsplit(".", 1)[0])
        search = list(getattr(parent, "__path__", None) or [])
        if not search or not str(search[0]).startswith(self.root):
            return None
        return importlib.machinery.PathFinder.find_spec(name, search)


def _unload() -> None:
    """Убирает скачанный код из поиска модулей и уже загруженные модули программы."""
    sys.meta_path[:] = [f for f in sys.meta_path if type(f).__name__ != "_Finder"]
    for m in [m for m in list(sys.modules) if m == PKG or m.startswith(PKG + ".")]:
        del sys.modules[m]


def active_build() -> str | None:
    """Номер подключённой скачанной сборки или None (работает встроенный код)."""
    return os.environ.get("WCL_CODE_BUILD") or None


def activate(log=print) -> str | None:
    """Подключает скачанный код, если он есть и подходит. Возвращает номер сборки или None."""
    os.environ.pop("WCL_CODE_BUILD", None)  # от прошлого подключения (мягкий перезапуск) — не тянуть
    os.environ.pop("WCL_CODE_PATH", None)
    root_dir = code_root()
    cur = root_dir / "current.json"
    if not cur.exists():
        return None
    try:
        info = json.loads(cur.read_text(encoding="utf-8"))
        root = str(Path(info["path"]))
        build = str(info.get("build"))
        if info.get("shell") != SHELL_ID or not (Path(root) / PKG / "__init__.py").exists():
            return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if build in failed_builds(root_dir):
        return None
    pending, confirmed = Path(root) / PENDING, Path(root) / CONFIRMED
    if not confirmed.exists():
        try:
            age = time.time() - pending.stat().st_mtime
        except OSError:
            age = None
        if age is not None and age > PENDING_GRACE_S:
            # прошлый запуск этой сборки не дошёл до работающего сервера — упал
            mark_failed(build, "прошлый запуск не дошёл до запуска сервера", root_dir)
            log(f"Скачанное обновление кода (сборка {build}) не запустилось в прошлый раз — "
                "работаю на встроенной версии.")
            return None
        if age is None:
            try:
                pending.write_text(str(int(time.time())), encoding="utf-8")
            except OSError:
                pass
    finder = _Finder(root)
    sys.meta_path.insert(0, finder)
    try:
        import wcl_analyzer  # noqa: F401
        import wcl_analyzer.web  # noqa: F401 — проверяем, что код целый
        os.environ["WCL_CODE_BUILD"] = build
        os.environ["WCL_CODE_PATH"] = root
        return build
    except Exception as e:  # noqa: BLE001 — сломанное обновление: работаем на встроенном коде
        _unload()
        mark_failed(build, f"не импортируется: {e}", root_dir)
        log(f"Скачанное обновление кода не запустилось ({e}) — работаю на встроенной версии.")
        return None


def fall_back(why="", log=print) -> bool:
    """Скачанный код упал во время работы: отключить его и вернуться к встроенному.

    Сборку, которая ещё ни разу не запустилась успешно (нет метки .confirmed), помечает сломанной.
    Возвращает True, если скачанный код был подключён (тогда стоит запустить программу ещё раз)."""
    build, path = active_build(), os.environ.get("WCL_CODE_PATH")
    if not build:
        return False
    if not (path and (Path(path) / CONFIRMED).exists()):
        mark_failed(build, f"ошибка при запуске: {why}")
    _unload()
    os.environ.pop("WCL_CODE_BUILD", None)
    os.environ.pop("WCL_CODE_PATH", None)
    log(f"Обновлённая версия кода (сборка {build}) завершилась с ошибкой ({why}) — запускаю встроенную версию.")
    return True

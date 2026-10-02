"""Загрузчик обновлений кода — часть «оболочки» (.exe / APK), сам не обновляется.

Программа умеет скачивать новую версию своего кода (пакет wcl_analyzer) в папку данных и запускать
её вместо встроенной — без переустановки. Этот модуль при запуске:
  1. смотрит, есть ли скачанный код и подходит ли он к этой оболочке (SHELL_ID — отпечаток
     версии Python, библиотек и настроек сборки; если оболочка другая, скачанный код не берётся);
  2. подключает его раньше встроенного (свой поисковик модулей — иначе PyInstaller отдаёт встроенный);
  3. если скачанный код не импортируется — откатывается на встроенный и помечает обновление сломанным.
"""
from __future__ import annotations

import importlib.machinery
import json
import os
import sys
from pathlib import Path

SHELL_ID = "dev"   # сборка на GitHub подставляет сюда отпечаток оболочки
PKG = "wcl_analyzer"


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


def activate(log=print) -> str | None:
    """Подключает скачанный код, если он есть и подходит. Возвращает номер сборки или None."""
    cur = code_root() / "current.json"
    if not cur.exists():
        return None
    try:
        info = json.loads(cur.read_text(encoding="utf-8"))
        root = str(Path(info["path"]))
        if info.get("shell") != SHELL_ID or not (Path(root) / PKG / "__init__.py").exists():
            return None
    except (OSError, ValueError, KeyError):
        return None
    finder = _Finder(root)
    sys.meta_path.insert(0, finder)
    try:
        import wcl_analyzer  # noqa: F401
        import wcl_analyzer.web  # noqa: F401 — проверяем, что код целый
        os.environ["WCL_CODE_BUILD"] = str(info.get("build"))
        return str(info.get("build"))
    except Exception as e:  # noqa: BLE001 — сломанное обновление: работаем на встроенном коде
        sys.meta_path.remove(finder)
        for m in [m for m in sys.modules if m == PKG or m.startswith(PKG + ".")]:
            del sys.modules[m]
        try:
            cur.rename(cur.with_name("failed.json"))
        except OSError:
            pass
        log(f"Скачанное обновление кода не запустилось ({e}) — работаю на встроенной версии.")
        return None

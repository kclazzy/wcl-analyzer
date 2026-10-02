"""Обновление программы из выпусков GitHub (без переустановки, если меняется только код).

Каждая сборка на GitHub публикует выпуск: APK, .exe, архив кода (пакет wcl_analyzer) и update.json
с номером сборки и отпечатком оболочки. Если оболочка та же (Python и библиотеки не менялись) —
скачивается только архив кода в папку данных; при следующем запуске его подключит wcl_boot.
Если оболочка новая — нужна новая программа целиком: .exe заменяется сам, APK открывается
для установки в браузере телефона.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

from ._build import BUILD
from ._build import SHELL_ID as _CODE_SHELL_ID

REPO = os.environ.get("WCL_UPDATE_REPO", "kclazzy/wcl-analyzer")
BASE = f"https://github.com/{REPO}/releases/latest/download/"
CODE_ZIP, APK, EXE = "wcl_analyzer-code.zip", "WCL-Analyzer.apk", "WCL-Analyzer.exe"


def _boot():
    try:
        import wcl_boot  # есть в .exe и APK; при запуске из исходников — нет
        return wcl_boot
    except ImportError:
        return None


def shell_id() -> str:
    b = _boot()
    return b.SHELL_ID if b else _CODE_SHELL_ID


def code_root() -> Path:
    b = _boot()
    if b:
        return b.code_root()
    from .config import data_dir
    return data_dir() / "code"


def _num(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def current() -> dict:
    from .platform_support import app_mode
    build = os.environ.get("WCL_CODE_BUILD") or BUILD
    return {"build": build, "shell_build": BUILD, "shell": shell_id(), "mode": app_mode(),
            "code_updated": bool(os.environ.get("WCL_CODE_BUILD")),
            "can_update": app_mode() in ("exe", "android") and shell_id() != "dev"}


def _get(url: str, timeout: float = 30) -> bytes:
    import requests
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content


def check() -> dict:
    """Есть ли новая сборка и какое обновление нужно: только код или программа целиком."""
    cur = current()
    info = json.loads(_get(BASE + "update.json", 15))
    new = _num(info.get("build"))
    have = _num(cur["build"])
    available = new > have
    kind = None
    if available:
        kind = "code" if info.get("shell") == cur["shell"] else "full"
    return {**cur, "latest": new, "available": available, "kind": kind, "date": info.get("date"),
            "page": f"https://github.com/{REPO}/releases/latest"}


def apply_code() -> dict:
    """Скачивает код новой сборки в папку данных. Подключится при следующем запуске программы."""
    info = json.loads(_get(BASE + "update.json", 15))
    if info.get("shell") != shell_id():
        raise ValueError("Для этой сборки нужна новая программа целиком, а не только код")
    data = _get(BASE + CODE_ZIP, 60)
    if info.get("sha256") and hashlib.sha256(data).hexdigest() != info["sha256"]:
        raise ValueError("Архив обновления повреждён (контрольная сумма не совпала) — попробуйте ещё раз")
    build = _num(info.get("build"))
    root = code_root()
    dest = root / f"build-{build}"
    tmp = root / f".tmp-{build}-{int(time.time())}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():  # только пакет wcl_analyzer, без выхода за пределы папки
            if not n.startswith("wcl_analyzer/") or ".." in Path(n).parts:
                raise ValueError(f"Неожиданный файл в архиве обновления: {n}")
        z.extractall(tmp)
    pkg = tmp / "wcl_analyzer"
    if not (pkg / "__init__.py").exists() or not (pkg / "web" / "index.html").exists():
        raise ValueError("В архиве обновления нет программы")
    for py in pkg.rglob("*.py"):  # синтаксис всех файлов — до того, как обновление будет включено
        compile(py.read_text(encoding="utf-8"), str(py), "exec")
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    (root / "current.json").write_text(json.dumps({"build": build, "shell": info["shell"], "path": str(dest)}),
                                       encoding="utf-8")
    for old in root.glob("build-*"):  # старые версии кода — кроме новой и той, что запущена сейчас
        if old != dest and old.name != f"build-{os.environ.get('WCL_CODE_BUILD')}":
            shutil.rmtree(old, ignore_errors=True)
    return {"ok": True, "build": build, "restart": restart_hint()}


def restart_hint() -> str:
    from .platform_support import app_mode
    return ("Программа перезапустится сама через пару секунд." if app_mode() == "exe"
            else "Закройте приложение (уберите из недавних) и откройте снова.")


def restart_exe(new_exe: Path | None = None) -> None:
    """Windows: перезапуск (и замена .exe, если скачан новый) после выхода этого процесса."""
    exe = Path(sys.executable)
    bat = exe.with_name("wcl_update.bat")
    move = (f'move /y "{new_exe}" "{exe}" >nul 2>&1 || (timeout /t 1 /nobreak >nul & goto again)\n'
            if new_exe else "")
    bat.write_text("@echo off\r\ntimeout /t 2 /nobreak >nul\r\n:again\r\n" + move.replace("\n", "\r\n")
                   + f'start "" "{exe}"\r\ndel "%~f0"\r\n', encoding="cp866", errors="replace")
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), close_fds=True)
    threading.Timer(1.0, lambda: os._exit(0)).start()


def apply_full() -> dict:
    """Новая программа целиком: .exe скачивается и заменяет себя; APK — ссылка для установки."""
    from .platform_support import app_mode
    mode = app_mode()
    if mode == "exe":
        data = _get(BASE + EXE, 300)
        new = Path(sys.executable).with_name("WCL Analyzer.new.exe")
        new.write_bytes(data)
        restart_exe(new)
        return {"ok": True, "restart": "Новая версия скачана, программа перезапустится через пару секунд."}
    if mode == "android":
        from .platform_support import android_open_url
        android_open_url(BASE + APK)
        return {"ok": True, "restart": "В браузере начнётся загрузка новой версии — установите её поверх."}
    return {"ok": False, "url": f"https://github.com/{REPO}/releases/latest"}

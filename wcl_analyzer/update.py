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
import re
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
API = f"https://api.github.com/repos/{REPO}/releases/latest"
# Запасной источник — jsDelivr, бесплатное зеркало файлов GitHub: доступен, когда сервер файлов GitHub — нет
JSD_DATA = f"https://data.jsdelivr.com/v1/packages/gh/{REPO}"
JSD_CDN = f"https://cdn.jsdelivr.net/gh/{REPO}"
CODE_ZIP, APK, EXE = "wcl_analyzer-code.zip", "WCL-Analyzer.apk", "WCL-Analyzer.exe"
# Файлы оболочки — по ним считается её отпечаток (как в сборке на GitHub)
SHELL_FILES = ["requirements.txt", "android/buildozer.spec", "android/main.py", "packaging/windows/wcl_app.py",
               "packaging/windows/wcl_analyzer.spec", "wcl_boot.py"]


class UpdateError(Exception):
    pass


def _why(e: Exception) -> str:
    """Коротко и по-русски: почему не открылся адрес."""
    import requests
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"ответ {e.response.status_code}"
    if isinstance(e, requests.Timeout):
        return "не отвечает (тайм-аут)"
    if isinstance(e, requests.exceptions.SSLError):
        return "ошибка защищённого соединения"
    if isinstance(e, requests.ConnectionError):
        return "нет соединения"
    return f"{type(e).__name__}: {e}"[:120]


def shell_fingerprint(texts: list[bytes]) -> str:
    """Отпечаток оболочки — как в сборке: без символов \r (на Windows окончания строк другие)."""
    return hashlib.sha256(b"".join(t.replace(b"\r", b"") for t in texts)).hexdigest()[:12]


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


def latest_info() -> dict:
    """Последняя сборка: с GitHub (файл выпуска или API), а если GitHub недоступен — через jsDelivr."""
    errors = []
    try:
        return {**json.loads(_get(BASE + "update.json", 15)), "source": "github"}
    except Exception as e:  # noqa: BLE001
        errors.append(f"github.com — {_why(e)}")
    try:
        rel = json.loads(_get(API, 15))
        m = re.search(r"<!--update (\{.*?\}) -->", rel.get("body") or "", re.S)
        if m:
            return {**json.loads(m.group(1)), "tag": rel.get("tag_name"), "source": "api"}
        errors.append("api.github.com — в выпуске нет данных о версии")
    except Exception as e:  # noqa: BLE001
        errors.append(f"api.github.com — {_why(e)}")
    try:
        ver = json.loads(_get(JSD_DATA + "/resolved?specifier=latest", 15))["version"]
        texts = [_get(f"{JSD_CDN}@{ver}/{f}", 15) for f in SHELL_FILES]
        return {"build": _num(str(ver).split(".")[-1]), "shell": shell_fingerprint(texts), "tag": ver,
                "source": "jsdelivr"}
    except Exception as e:  # noqa: BLE001
        errors.append(f"jsdelivr.net — {_why(e)}")
    raise UpdateError("Сервер обновлений недоступен с этого устройства: " + "; ".join(errors))


def check() -> dict:
    """Есть ли новая сборка и какое обновление нужно: только код или программа целиком."""
    cur = current()
    info = latest_info()
    new = _num(info.get("build"))
    have = _num(cur["build"])
    available = new > have
    kind = None
    if available:
        kind = "code" if info.get("shell") == cur["shell"] else "full"
    return {**cur, "latest": new, "available": available, "kind": kind, "date": info.get("date"),
            "source": info.get("source"), "page": f"https://github.com/{REPO}/releases/latest"}


def apply_code() -> dict:
    """Скачивает код новой сборки в папку данных. Подключится при следующем запуске программы."""
    info = latest_info()
    if info.get("shell") != shell_id():
        raise ValueError("Для этой сборки нужна новая программа целиком, а не только код")
    build = _num(info.get("build"))
    root = code_root()
    dest = root / f"build-{build}"
    tmp = root / f".tmp-{build}-{int(time.time())}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    data = None
    if info.get("source") == "github":
        try:
            data = _get(BASE + CODE_ZIP, 60)
        except Exception:  # noqa: BLE001 — архив с GitHub не скачался: берём файлы через jsDelivr
            data = None
    if data is not None:
        if info.get("sha256") and hashlib.sha256(data).hexdigest() != info["sha256"]:
            raise ValueError("Архив обновления повреждён (контрольная сумма не совпала) — попробуйте ещё раз")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():  # только пакет wcl_analyzer, без выхода за пределы папки
                if not n.startswith("wcl_analyzer/") or ".." in Path(n).parts:
                    raise ValueError(f"Неожиданный файл в архиве обновления: {n}")
            z.extractall(tmp)
    else:
        _download_jsdelivr(info, tmp)
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


def _download_jsdelivr(info: dict, tmp: Path) -> None:
    """Код сборки по файлам через jsDelivr (если архив с GitHub не скачивается)."""
    ver = info.get("tag") or f"v1.1.{info.get('build')}"
    listing = json.loads(_get(f"{JSD_DATA}@{ver}?structure=flat", 30))
    names = [f["name"] for f in listing.get("files") or []
             if f["name"].startswith("/wcl_analyzer/") and "__pycache__" not in f["name"] and ".." not in f["name"]]
    if not names:
        raise UpdateError("В зеркале jsDelivr нет файлов программы для этой версии")
    for n in names:
        target = tmp / n.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_get(f"{JSD_CDN}@{ver}{n}", 30))
    # номер сборки и отпечаток оболочки — как их проставляет сборка на GitHub
    (tmp / "wcl_analyzer" / "_build.py").write_text(
        f'"""Номер сборки и отпечаток оболочки (обновление через jsDelivr)."""\nBUILD = "{info.get("build")}"\n'
        f'SHELL_ID = "{info.get("shell")}"\n', encoding="utf-8")


def restart_hint() -> str:
    return "Программа перезапустится сама через пару секунд."


def restart_exe(new_exe: Path | None = None) -> None:
    """Windows: перезапуск (и замена .exe, если скачан новый) после выхода этого процесса."""
    exe = Path(sys.executable)
    bat = exe.with_name("wcl_update.bat")
    move = (f'move /y "{new_exe}" "{exe}" >nul 2>&1 || (timeout /t 1 /nobreak >nul & goto again)\n'
            if new_exe else "")
    # WCL_NO_BROWSER: новая программа не открывает ещё одну вкладку — открытая страница переподключится сама
    bat.write_text("@echo off\r\nset WCL_NO_BROWSER=1\r\ntimeout /t 2 /nobreak >nul\r\n:again\r\n" + move.replace("\n", "\r\n")
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
        return {"ok": True, "restart": "Новая версия скачана, программа перезапустится через пару секунд.",
                "full_restart": True}
    if mode == "android":
        from .platform_support import android_open_url
        android_open_url(BASE + APK)
        return {"ok": True, "restart": "В браузере начнётся загрузка новой версии — установите её поверх."}
    return {"ok": False, "url": f"https://github.com/{REPO}/releases/latest"}

"""Обновление программы из выпусков GitHub (без переустановки, если меняется только код).

Каждая сборка на GitHub публикует выпуск: APK, .exe, архив кода (пакет wcl_analyzer) и update.json
с номером сборки и отпечатком оболочки. Если оболочка та же (Python и библиотеки не менялись) —
скачивается только архив кода в папку данных; при следующем запуске его подключит wcl_boot.
Если оболочка новая — нужна новая программа целиком: .exe заменяется сам, APK открывается
для установки в браузере телефона.

Проверки: архив кода — по sha256 из update.json; код через jsDelivr — по отпечатку всех файлов
пакета (code_sha256); новый .exe — по exe_sha256 и размеру (в update.json формата 2 они обязательны).
Новая скачанная сборка кода сначала «ждёт подтверждения» (см. wcl_boot): веб-сервер, запустившись,
вызывает confirm_running_build(); сборка, которая так и не дошла до сервера, больше не запускается.
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
import tempfile
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


# Метки «ждёт подтверждения» / «подтверждена» в папке сборки и список сломанных — как в wcl_boot
PENDING, CONFIRMED, FAILED = ".pending", ".confirmed", "failed.json"


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


def _offline(e: Exception) -> bool:
    """Нет связи (нет интернета, адрес не найден, тайм-аут) — а не ответ сервера с ошибкой."""
    import requests
    return isinstance(e, (requests.ConnectionError, requests.Timeout))


def latest_info() -> dict:
    """Последняя сборка: с GitHub (файл выпуска или API), а если GitHub недоступен — через jsDelivr.
    Вся проверка укладывается в ~25 с: без интернета страница сразу получает понятный ответ."""
    errors, offline = [], []
    try:
        return {**json.loads(_get(BASE + "update.json", 8)), "source": "github"}
    except Exception as e:  # noqa: BLE001
        errors.append(f"github.com — {_why(e)}")
        offline.append(_offline(e))
    if not offline[-1]:   # github.com отвечает, но файла нет — пробуем API; нет связи с GitHub вовсе — сразу зеркало
        try:
            rel = json.loads(_get(API, 8))
            m = re.search(r"<!--update (\{.*?\}) -->", rel.get("body") or "", re.S)
            if m:
                return {**json.loads(m.group(1)), "tag": rel.get("tag_name"), "source": "api"}
            errors.append("api.github.com — в выпуске нет данных о версии")
            offline.append(False)
        except Exception as e:  # noqa: BLE001
            errors.append(f"api.github.com — {_why(e)}")
            offline.append(_offline(e))
    try:
        ver = json.loads(_get(JSD_DATA + "/resolved?specifier=latest", 8))["version"]
        texts = [_get(f"{JSD_CDN}@{ver}/{f}", 8) for f in SHELL_FILES]
        return {"build": _num(str(ver).split(".")[-1]), "shell": shell_fingerprint(texts), "tag": ver,
                "source": "jsdelivr"}
    except Exception as e:  # noqa: BLE001
        errors.append(f"jsdelivr.net — {_why(e)}")
        offline.append(_offline(e))
    if all(offline):
        raise UpdateError("Нет связи с интернетом — проверить обновления не получилось. "
                          "Подключитесь к сети и нажмите «Проверить обновления» ещё раз. "
                          f"(Подробности: {'; '.join(errors)}.)")
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
    failed = kind == "code" and str(new) in _failed_builds(code_root())
    if failed:  # эта сборка кода здесь уже не запустилась — ждём следующую
        available, kind = False, None
    return {**cur, "latest": new, "available": available, "kind": kind, "date": info.get("date"),
            "source": info.get("source"), "page": f"https://github.com/{REPO}/releases/latest",
            **({"failed_build": new} if failed else {})}


def _failed_builds(root: Path) -> set[str]:
    """Сборки кода, которые на этом устройстве не запустились (формат failed.json — как в wcl_boot)."""
    try:
        d = json.loads((root / FAILED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(d, dict):
        return set()
    out = {str(b) for b in d.get("builds") or [] if b is not None}
    if d.get("build") is not None:
        out.add(str(d["build"]))
    return out


def _in_use_dir() -> Path | None:
    """Папка скачанного кода, на котором программа работает сейчас (её трогать нельзя)."""
    if os.environ.get("WCL_CODE_PATH"):
        return Path(os.environ["WCL_CODE_PATH"])
    if os.environ.get("WCL_CODE_BUILD"):
        return code_root() / f"build-{os.environ['WCL_CODE_BUILD']}"
    return None


def _same(a: Path | None, b: Path | None) -> bool:
    if a is None or b is None:
        return False
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


def confirm_running_build() -> bool:
    """Веб-сервер запустился: скачанная сборка кода, на которой работает программа, исправна.

    Вызывать один раз, когда сервер уже слушает порт. Без скачанного кода ничего не делает.
    Возвращает True, если сборка подтверждена (метка .confirmed поставлена)."""
    path = _in_use_dir()
    if path is None or not path.is_dir():
        return False
    try:
        (path / CONFIRMED).write_text(str(int(time.time())), encoding="utf-8")
        (path / PENDING).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def code_tree_hash(pkg: Path) -> str:
    """Отпечаток кода — все файлы пакета, кроме _build.py и __pycache__ (так же считает сборка на GitHub)."""
    pkg = Path(pkg)
    items = []
    for f in pkg.rglob("*"):
        rel = f.relative_to(pkg)
        if not f.is_file() or "__pycache__" in rel.parts or f.suffix == ".pyc" or rel.as_posix() == "_build.py":
            continue
        items.append((rel.as_posix(), hashlib.sha256(f.read_bytes()).hexdigest()))
    h = hashlib.sha256()
    for name, digest in sorted(items):
        h.update(f"{name}\0{digest}\n".encode("utf-8"))
    return h.hexdigest()


def apply_code() -> dict:
    """Скачивает код новой сборки в папку данных. Подключится при следующем запуске программы."""
    info = latest_info()
    if info.get("shell") != shell_id():
        raise ValueError("Для этой сборки нужна новая программа целиком, а не только код")
    build = _num(info.get("build"))
    have = _num(current()["build"])
    if build <= have:  # такая или более новая сборка уже работает — скачивать нечего
        return {"ok": False, "build": build, "up_to_date": True,
                "error": f"Уже работает сборка {have} — она не старше сборки {build}, обновлять нечего"}
    root = code_root()
    if str(build) in _failed_builds(root):
        raise ValueError(f"Сборка {build} уже не запустилась на этом устройстве — дождитесь следующей "
                         "или установите программу целиком")
    dest = root / f"build-{build}"
    in_use = _in_use_dir()
    if _same(dest, in_use):
        return {"ok": False, "build": build, "up_to_date": True, "error": f"Сборка {build} уже работает"}
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
    shutil.rmtree(dest, ignore_errors=True)  # недокачанная прежняя попытка (не та, что работает, — см. выше)
    tmp.rename(dest)
    (root / "current.json").write_text(json.dumps({"build": build, "shell": info["shell"], "path": str(dest)}),
                                       encoding="utf-8")
    for old in list(root.glob("build-*")) + list(root.glob(".tmp-*")):
        # старые версии кода и брошенные загрузки — кроме новой и той, что запущена сейчас
        if not _same(old, dest) and not _same(old, in_use) and old != tmp:
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
    want = info.get("code_sha256")
    if want and code_tree_hash(tmp / "wcl_analyzer") != want:
        raise ValueError("Код обновления из зеркала jsDelivr повреждён (контрольная сумма не совпала) — "
                         "попробуйте ещё раз позже")
    # номер сборки и отпечаток оболочки — как их проставляет сборка на GitHub
    (tmp / "wcl_analyzer" / "_build.py").write_text(
        f'"""Номер сборки и отпечаток оболочки (обновление через jsDelivr)."""\nBUILD = "{info.get("build")}"\n'
        f'SHELL_ID = "{info.get("shell")}"\n', encoding="utf-8")


def restart_hint() -> str:
    return "Программа перезапустится сама через пару секунд."


# Скрипт перезапуска .exe — только латиница: пути приходят через переменные окружения (WCL_OLD_EXE,
# WCL_NEW_EXE), поэтому папки с русскими буквами работают при любой кодовой странице консоли.
# Паузы — через ping: timeout без окна консоли (а скрипт скрыт) сразу завершается с ошибкой.
# Замена .exe — до 30 попыток (~30 с), пока старая программа не закроется; не вышло — запускается старая.
RESTART_BAT = "\r\n".join([
    "@echo off",
    "setlocal",
    "set /a tries=0",
    "ping -n 3 127.0.0.1 >nul",
    "if not defined WCL_NEW_EXE goto run",
    ":again",
    'move /y "%WCL_NEW_EXE%" "%WCL_OLD_EXE%" >nul 2>&1 && goto run',
    "set /a tries+=1",
    "if %tries% geq 30 goto failed",
    "ping -n 2 127.0.0.1 >nul",
    "goto again",
    ":failed",
    'del /f /q "%WCL_NEW_EXE%" >nul 2>&1',
    ":run",
    'start "" "%WCL_OLD_EXE%" %WCL_ARGS%',
    '(goto) 2>nul & del "%~f0"',
    "",
])


def _restart_args() -> str:
    """Те же параметры запуска (например, --port), что у работающей программы: страница ждёт её
    по тому же адресу. Только простые аргументы — без кавычек и спецсимволов командной строки."""
    import re as _re
    args = [a for a in sys.argv[1:] if _re.fullmatch(r"[\w.:=/-]+", a)]
    return " ".join(args)


def _restart_env(exe: Path, new_exe: Path | None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("WCL_CODE_BUILD", "WCL_CODE_PATH", "WCL_NEW_EXE", "_MEIPASS2") and not k.startswith("_PYI")}
    # WCL_NO_BROWSER: новая программа не открывает ещё одну вкладку — открытая страница переподключится сама.
    # PYINSTALLER_RESET_ENVIRONMENT: новый .exe — отдельная программа, а не дочерний процесс этой
    env.update(WCL_NO_BROWSER="1", WCL_OLD_EXE=str(exe), PYINSTALLER_RESET_ENVIRONMENT="1", WCL_ARGS=_restart_args())
    if new_exe:
        env["WCL_NEW_EXE"] = str(new_exe)
    return env


def restart_exe(new_exe: Path | None = None) -> None:
    """Windows: перезапуск (и замена .exe, если скачан новый) после выхода этого процесса."""
    exe = Path(sys.executable)
    bat = Path(tempfile.gettempdir()) / f"wcl_update_{os.getpid()}.bat"
    bat.write_text(RESTART_BAT, encoding="ascii", newline="")
    subprocess.Popen(["cmd", "/c", str(bat)], env=_restart_env(exe, new_exe),
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), close_fds=True)
    threading.Timer(1.0, lambda: os._exit(0)).start()


def verify_download(info: dict, data: bytes, kind: str) -> None:
    """Скачанная программа целиком (kind: exe | apk) совпадает с той, что опубликована в выпуске.

    Контрольная сумма проверяется, если она известна (update.json или описание выпуска). В update.json
    формата 2 она обязательна; номер сборки через jsDelivr приходит без неё — тогда проверить нечем."""
    want = (info.get(f"{kind}_sha256") or "").lower()
    size = _num(info.get(f"{kind}_size"))
    if not want:
        if _num(info.get("format")) >= 2:
            raise UpdateError("В данных обновления нет контрольной суммы программы — обновление отменено. "
                              "Скачайте программу со страницы выпуска вручную.")
        return
    if size and len(data) != size:
        raise UpdateError(f"Программа скачалась не полностью ({len(data)} из {size} байт) — "
                          "обновление отменено, попробуйте ещё раз")
    if hashlib.sha256(data).hexdigest() != want:
        raise UpdateError("Скачанная программа повреждена (контрольная сумма не совпала) — "
                          "обновление отменено, попробуйте ещё раз")


def apply_full() -> dict:
    """Новая программа целиком: .exe скачивается, проверяется и заменяет себя; APK — ссылка для установки."""
    from .platform_support import app_mode
    mode = app_mode()
    if mode == "exe":
        info = latest_info()
        data = _get(BASE + EXE, 300)
        verify_download(info, data, "exe")  # до записи на диск: повреждённая программа не заменит рабочую
        new = Path(sys.executable).with_name("WCL Analyzer.new.exe")
        new.write_bytes(data)
        restart_exe(new)
        return {"ok": True, "restart": "Новая версия скачана, программа перезапустится через пару секунд.",
                "full_restart": True}
    if mode == "android":
        # APK скачивает и устанавливает браузер; Android сам проверяет подпись: APK с другим ключом
        # или повреждённый поверх установленного приложения не встанет
        from .platform_support import android_open_url
        android_open_url(BASE + APK)
        return {"ok": True, "restart": "В браузере начнётся загрузка новой версии — установите её поверх."}
    return {"ok": False, "url": f"https://github.com/{REPO}/releases/latest"}

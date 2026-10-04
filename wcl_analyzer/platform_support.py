"""Особенности платформ: Windows-программа (.exe), Android-приложение, обычный Python.

- где хранить данные программы (кэш публичных данных WCL, настройки);
- Android: сохранение файлов в «Загрузки» и открытие ссылок в браузере телефона —
  встроенное окно приложения само этого не умеет.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def app_mode() -> str:
    if os.environ.get("WCL_APP") == "android" or "ANDROID_PRIVATE" in os.environ or "ANDROID_ARGUMENT" in os.environ:
        return "android"
    if getattr(sys, "frozen", False):
        return "exe"
    return "python"


def default_data_dir() -> Path:
    mode = app_mode()
    if mode == "android":
        return Path(os.environ.get("ANDROID_PRIVATE") or os.getcwd()) / "wcl-data"
    if mode == "exe":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "WCL Analyzer"
    return Path(".")


# ---------------------------------------------------------------- Android
# Классы Java нужно загрузить в главном потоке: потоки веб-сервера (где обрабатываются нажатия)
# не видят классы самого приложения — autoclass("org.kivy.android.PythonActivity") там даёт
# ClassNotFoundException. Поэтому serve() вызывает android_preload() при запуске, а дальше
# функции берут уже загруженные классы.
_J: dict = {}
_JAVA = {
    "PythonActivity": "org.kivy.android.PythonActivity",
    "Build": "android.os.Build$VERSION",
    "MediaStore": "android.provider.MediaStore$Downloads",
    "ContentValues": "android.content.ContentValues",
    "Environment": "android.os.Environment",
    "Intent": "android.content.Intent",
    "Uri": "android.net.Uri",
}


def android_preload() -> None:
    """Загружает нужные классы Java (вызывать в главном потоке, до запуска сервера)."""
    from jnius import autoclass  # есть только внутри Android-приложения
    for key, name in _JAVA.items():
        if key in _J:
            continue
        try:
            _J[key] = autoclass(name)
        except Exception:  # noqa: BLE001 — например, MediaStore$Downloads нет на Android 9 и старше
            pass
    act = _J.get("PythonActivity")
    if act is not None:
        _J["activity"] = act.mActivity


def _j(key: str):
    if key not in _J:
        android_preload()  # запасной путь: если вызвано до preload (в главном потоке сработает)
    if key not in _J:
        raise OSError(f"Android: класс {_JAVA.get(key, _JAVA['PythonActivity'] if key == 'activity' else key)} "
                      "недоступен — перезапустите приложение")
    return _J[key]


def android_save_download(name: str, data: bytes, mime: str, sub: str = "") -> str:
    """Сохраняет файл в общую папку «Загрузки» телефона (подпапка sub, по умолчанию «WCL Analyzer»).
    Возвращает, где искать файл."""
    import re
    sub = re.sub(r'[\\:*?"<>|]+', "_", sub or "").strip(" /") or "WCL Analyzer"
    Build = _j("Build")
    activity = _j("activity")
    if Build.SDK_INT >= 29:
        MediaStore = _j("MediaStore")
        ContentValues = _j("ContentValues")
        values = ContentValues()
        values.put("_display_name", name)
        values.put("mime_type", mime)
        values.put("relative_path", f"Download/{sub}")
        resolver = activity.getContentResolver()
        uri = resolver.insert(MediaStore.EXTERNAL_CONTENT_URI, values)
        if uri is None:
            raise OSError("Android не разрешил создать файл в «Загрузках»")
        stream = resolver.openOutputStream(uri)
        try:
            stream.write(bytearray(data))
        finally:
            stream.close()
        return f"Загрузки/{sub}/{name}"
    # Android 7–9: личная папка приложения на общем хранилище — разрешения не нужны
    Environment = _j("Environment")
    folder = Path(activity.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS).getAbsolutePath())
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    return f"{folder}/{name}"


def android_open_url(url: str) -> None:
    """Открывает ссылку в браузере телефона (Warcraft Logs, страница создания ключа)."""
    Intent, Uri = _j("Intent"), _j("Uri")
    activity = _j("activity")
    intent = Intent(Intent.ACTION_VIEW, Uri.parse(url))
    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    activity.startActivity(intent)


def downloads_dir() -> Path:
    """Папка «Загрузки» пользователя полным путём (Windows — настоящая, даже если её перенесли на другой диск)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]
            # FOLDERID_Downloads {374DE290-123F-4565-9164-39C4925E467B}
            fid = GUID(0x374DE290, 0x123F, 0x4565, (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
            p = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(p)) == 0:
                out = Path(p.value)
                ctypes.windll.ole32.CoTaskMemFree(p)
                return out
        except Exception:  # noqa: BLE001 — запасной путь ниже
            pass
    return Path.home() / "Downloads"


def pick_folder(start: str = "") -> str | None:
    """Системное окно выбора папки на этом компьютере → полный путь; отмена — None.
    Windows — окно проводника (PowerShell), macOS — Finder, Linux — zenity/kdialog. Нет такого окна — OSError."""
    import shutil
    import subprocess
    if os.name == "nt":
        ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;Add-Type -AssemblyName System.Windows.Forms;"
              "$d=New-Object System.Windows.Forms.FolderBrowserDialog;$d.Description='WCL Analyzer: куда сохранять файлы';"
              "$d.ShowNewFolderButton=$true;$d.SelectedPath=$env:WCL_START;"
              "$f=New-Object System.Windows.Forms.Form -Property @{TopMost=$true};"
              "if($d.ShowDialog($f) -eq 'OK'){$d.SelectedPath}")
        cmd = ["powershell", "-NoProfile", "-STA", "-Command", ps]
    elif sys.platform == "darwin":
        cmd = ["osascript", "-e", 'POSIX path of (choose folder with prompt "WCL Analyzer: куда сохранять файлы")']
    elif shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", "--directory", "--title=WCL Analyzer: куда сохранять файлы"]
    elif shutil.which("kdialog"):
        cmd = ["kdialog", "--getexistingdirectory", start or str(Path.home())]
    else:
        raise OSError("на этом компьютере нет окна выбора папки — впишите путь вручную")
    env = {**os.environ, "WCL_START": start or ""}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = subprocess.run(cmd, capture_output=True, timeout=600, env=env, creationflags=flags)
    out = r.stdout.decode("utf-8", "replace").strip()
    return out or None


def open_folder(path: str) -> None:
    """Открыть папку в проводнике / Finder."""
    import subprocess
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(p))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])

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
def _android():
    from jnius import autoclass  # есть только внутри Android-приложения
    return autoclass


def android_save_download(name: str, data: bytes, mime: str) -> str:
    """Сохраняет файл в общую папку «Загрузки» телефона. Возвращает, где искать файл."""
    autoclass = _android()
    Build = autoclass("android.os.Build$VERSION")
    activity = autoclass("org.kivy.android.PythonActivity").mActivity
    if Build.SDK_INT >= 29:
        MediaStore = autoclass("android.provider.MediaStore$Downloads")
        ContentValues = autoclass("android.content.ContentValues")
        values = ContentValues()
        values.put("_display_name", name)
        values.put("mime_type", mime)
        values.put("relative_path", "Download/WCL Analyzer")
        resolver = activity.getContentResolver()
        uri = resolver.insert(MediaStore.EXTERNAL_CONTENT_URI, values)
        if uri is None:
            raise OSError("Android не разрешил создать файл в «Загрузках»")
        stream = resolver.openOutputStream(uri)
        try:
            stream.write(bytearray(data))
        finally:
            stream.close()
        return f"Загрузки/WCL Analyzer/{name}"
    # Android 7–9: личная папка приложения на общем хранилище — разрешения не нужны
    Environment = autoclass("android.os.Environment")
    folder = Path(activity.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS).getAbsolutePath())
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    return f"{folder}/{name}"


def android_open_url(url: str) -> None:
    """Открывает ссылку в браузере телефона (Warcraft Logs, страница создания ключа)."""
    autoclass = _android()
    Intent, Uri = autoclass("android.content.Intent"), autoclass("android.net.Uri")
    activity = autoclass("org.kivy.android.PythonActivity").mActivity
    intent = Intent(Intent.ACTION_VIEW, Uri.parse(url))
    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    activity.startActivity(intent)

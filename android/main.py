"""Android-приложение: внутри телефона запускается тот же сервер анализа,
а встроенное окно приложения показывает интерфейс (http://127.0.0.1:8765).

Все данные — ключ API, эталоны, история разборов — хранятся на телефоне:
в памяти встроенного окна и в личной папке приложения. Excel и резервные
копии сохраняются в «Загрузки/WCL Analyzer».
"""
import os
import sys
import traceback

os.environ["WCL_APP"] = "android"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from wcl_analyzer.web import serve
    serve(port=8765, open_browser=False, local_only=True)
except Exception:  # ошибка запуска видна в журнале Android (adb logcat)
    traceback.print_exc()
    raise

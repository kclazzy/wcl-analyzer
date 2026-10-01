#!/usr/bin/env bash
# Запуск WCL Analyzer на macOS и Linux: двойной щелчок или ./start_mac.command
cd "$(dirname "$0")" || exit 1

PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Нужен Python 3.10 или новее: https://www.python.org/downloads/"
  read -r -p "Нажмите Enter, чтобы закрыть окно…"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Первый запуск: готовлю окружение, это займёт минуту…"
  "$PY" -m venv .venv || { echo "Не удалось создать окружение Python."; read -r; exit 1; }
fi

.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt \
  || { echo "Не удалось установить зависимости. Проверьте интернет."; read -r; exit 1; }

exec .venv/bin/python -m wcl_analyzer ui

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

# Зависимости ставятся только при первом запуске и когда изменился requirements.txt
# (его отпечаток хранится в .venv) — без интернета программа запускается как обычно
REQ_HASH="$(.venv/bin/python -c 'import hashlib; print(hashlib.sha256(open("requirements.txt", "rb").read()).hexdigest())')"
if [ "$(cat .venv/requirements.sha256 2>/dev/null)" != "$REQ_HASH" ]; then
  if .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt; then
    echo "$REQ_HASH" > .venv/requirements.sha256
  elif .venv/bin/python -c "import wcl_analyzer.web" 2>/dev/null; then
    echo "Внимание: не удалось обновить зависимости (нет интернета?) — запускаю с уже установленными."
  else
    echo "Не удалось установить зависимости. Проверьте интернет."; read -r; exit 1
  fi
fi

exec .venv/bin/python -m wcl_analyzer ui

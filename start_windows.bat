@echo off
chcp 65001 >nul
title WCL Analyzer
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo Не найден Python.
  echo Установите Python 3.10 или новее с https://www.python.org/downloads/
  echo и при установке отметьте "Add python.exe to PATH". Затем запустите этот файл снова.
  start "" https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Первый запуск: готовлю окружение, это займёт минуту...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Не удалось создать окружение Python.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo Не удалось установить зависимости. Проверьте интернет и запустите снова.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m wcl_analyzer ui
pause

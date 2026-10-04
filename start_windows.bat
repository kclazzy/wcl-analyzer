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

rem Зависимости ставятся только при первом запуске и когда изменился requirements.txt
rem (его отпечаток хранится в .venv) - без интернета программа запускается как обычно
set "VPY=.venv\Scripts\python.exe"
"%VPY%" -c "import hashlib, pathlib, sys; h = hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest(); p = pathlib.Path('.venv/requirements.sha256'); sys.exit(0 if p.exists() and p.read_text().strip() == h else 1)"
if not errorlevel 1 goto run

"%VPY%" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto pipfail
"%VPY%" -c "import hashlib, pathlib; pathlib.Path('.venv/requirements.sha256').write_text(hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest())"
goto run

:pipfail
"%VPY%" -c "import wcl_analyzer.web" >nul 2>nul
if errorlevel 1 (
  echo Не удалось установить зависимости. Проверьте интернет и запустите снова.
  pause
  exit /b 1
)
echo Внимание: не удалось обновить зависимости ^(нет интернета?^) - запускаю с уже установленными.

:run

"%VPY%" -m wcl_analyzer ui
pause

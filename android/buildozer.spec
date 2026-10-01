# Сборка Android-приложения (APK). Обычно её выполняет GitHub Actions (.github/workflows/build.yml).
# Вручную на Linux: cp -r ../wcl_analyzer . && buildozer android debug
[app]
title = Разбор WCL
package.name = wclanalyzer
package.domain = org.wclanalyzer
source.dir = .
source.include_exts = py,html,js,png,json
source.exclude_dirs = bin,.buildozer,__pycache__
version = 1.0.0

# Python и библиотеки анализа; numpy, pyjnius и openssl собираются рецептами python-for-android
requirements = python3,openssl,requests,urllib3,certifi,charset-normalizer,idna,openpyxl,et_xmlfile,numpy,segno,pyjnius

# Встроенное окно браузера показывает интерфейс сервера, запущенного внутри приложения
p4a.bootstrap = webview
# Стабильный выпуск python-for-android: свежая ветка сборщика меняется и ломает сборку
p4a.branch = v2024.01.21
p4a.port = 8765

icon.filename = %(source.dir)s/wcl_analyzer/web/icon-512.png
orientation = portrait
fullscreen = 0

android.permissions = INTERNET
android.api = 34
android.minapi = 24
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True
android.allow_backup = False

[buildozer]
log_level = 2
warn_on_root = 0

# PyInstaller: один файл «WCL Analyzer.exe», Python внутри.
# Сборка (на Windows): pyinstaller packaging/windows/wcl_analyzer.spec
import os
ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

a = Analysis(
    [os.path.join(SPECPATH, "wcl_app.py")],
    pathex=[ROOT],
    datas=[
        (os.path.join(ROOT, "wcl_analyzer", "web"), os.path.join("wcl_analyzer", "web")),
        (os.path.join(ROOT, "wcl_analyzer", "data"), os.path.join("wcl_analyzer", "data")),
        (os.path.join(ROOT, "spell_meta.example.json"), "."),
    ],
    hiddenimports=["openpyxl"],
    # numpy больше не нужен (статистика — свой модуль stats.py): десятки МБ меньше и быстрее запуск
    excludes=["tkinter", "matplotlib", "pandas", "scipy", "PIL", "pytest", "numpy"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="WCL Analyzer",
    icon=os.path.join(SPECPATH, "icon.ico") if os.name == "nt" else None,
    console=True,        # окно с подсказкой «не закрывайте, пока пользуетесь»
    upx=False,
)

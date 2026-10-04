"""Обновление программы без сети: скачивание кода, откат сломанной сборки, проверка .exe.

Запуск: python tests/test_update.py (или pytest tests/test_update.py)
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wcl_analyzer import platform_support, update  # noqa: E402

SHELL = "testshell02"


def _pkg_files(build: int, web: str = "def serve(**kw):\n    pass\n") -> dict[str, bytes]:
    """Маленький пакет wcl_analyzer — как в архиве кода сборки."""
    return {
        "wcl_analyzer/__init__.py": b"",
        "wcl_analyzer/_build.py": f'BUILD = "{build}"\nSHELL_ID = "{SHELL}"\n'.encode(),
        "wcl_analyzer/web.py": web.encode(),
        "wcl_analyzer/web/index.html": b"<html></html>",
    }


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


@contextlib.contextmanager
def _patched(info: dict | None = None, code: bytes = b"", env: dict | None = None, calls: list | None = None):
    """Подмена загрузки (_get), оболочки, папки кода и переменных окружения — без сети."""
    tmp = Path(tempfile.mkdtemp())
    old = (update._get, update.shell_id, update.code_root)
    old_env = {k: os.environ.get(k) for k in ("WCL_CODE_BUILD", "WCL_CODE_PATH")}

    def fake_get(url, timeout=30):
        if calls is not None:
            calls.append(url)
        if url.endswith("update.json"):
            return json.dumps(info).encode()
        return code

    update._get, update.shell_id, update.code_root = fake_get, (lambda: SHELL), (lambda: tmp / "code")
    for k in old_env:
        os.environ.pop(k, None)
    os.environ.update(env or {})
    try:
        yield tmp / "code"
    finally:
        update._get, update.shell_id, update.code_root = old
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _info(build: int, code: bytes, **extra) -> dict:
    return {"build": build, "shell": SHELL, "sha256": hashlib.sha256(code).hexdigest(), **extra}


def test_apply_code_skips_old_and_keeps_running_dir():
    """Сборка не новее работающей — ничего не скачивается; папка работающей сборки не удаляется."""
    code = _zip(_pkg_files(90))
    calls: list = []
    with _patched(_info(90, code), code, calls=calls) as root:
        running = root / "build-90"
        running.mkdir(parents=True)
        (running / "marker").write_text("в работе")
        os.environ.update(WCL_CODE_BUILD="90", WCL_CODE_PATH=str(running))
        res = update.apply_code()
        assert not res["ok"] and res.get("up_to_date"), res
        assert (running / "marker").exists(), "папка работающей сборки удалена"
        assert not any(u.endswith(update.CODE_ZIP) for u in calls), "архив скачан зря"
        # сборка старше работающей — тоже ничего
        old = _zip(_pkg_files(85))
        update._get = lambda url, timeout=30: json.dumps(_info(85, old)).encode() if url.endswith("update.json") else old
        res = update.apply_code()
        assert not res["ok"] and (running / "marker").exists(), res
        # новая сборка ставится, работающая остаётся, совсем старая удаляется
        (root / "build-70").mkdir()
        new = _zip(_pkg_files(91))
        update._get = lambda url, timeout=30: json.dumps(_info(91, new)).encode() if url.endswith("update.json") else new
        res = update.apply_code()
        assert res["ok"] and res["build"] == 91, res
        assert (running / "marker").exists(), "папка работающей сборки удалена при установке новой"
        assert not (root / "build-70").exists(), "старая сборка не удалена"
        cur = json.loads((root / "current.json").read_text(encoding="utf-8"))
        assert cur["build"] == 91 and Path(cur["path"]).name == "build-91", cur
    print("OK обновление кода: старая и та же сборка не ставятся, работающая папка не удаляется")


def test_zip_escape_rejected():
    """Архив с файлами вне пакета (../, чужая папка) не принимается."""
    for bad in ("wcl_analyzer/../evil.py", "other/x.py"):
        code = _zip({**_pkg_files(92), bad: b"print('x')\n"})
        with _patched(_info(92, code), code) as root:
            try:
                update.apply_code()
                raise AssertionError(f"принят архив с {bad}")
            except ValueError as e:
                assert "Неожиданный файл" in str(e), e
            assert not (root / "current.json").exists()
            assert not (root.parent / "evil.py").exists()
    print("OK обновление кода: архив с выходом за пределы пакета отклонён")


def test_failed_build_not_retried():
    """Сборка, которая здесь уже не запустилась, не предлагается и не скачивается снова."""
    code = _zip(_pkg_files(95))
    with _patched(_info(95, code), code) as root:
        root.mkdir(parents=True)
        (root / "failed.json").write_text(json.dumps({"build": "95", "builds": ["95"]}), encoding="utf-8")
        chk = update.check()
        assert not chk["available"] and chk.get("failed_build") == 95, chk
        try:
            update.apply_code()
            raise AssertionError("сломанная сборка скачана снова")
        except ValueError as e:
            assert "не запустилась" in str(e), e
        # более новая сборка — как обычно
        new = _zip(_pkg_files(96))
        update._get = lambda url, timeout=30: json.dumps(_info(96, new)).encode() if url.endswith("update.json") else new
        assert update.check()["available"] and update.apply_code()["ok"]
    print("OK обновление кода: сломанная сборка не повторяется, более новая ставится")


def test_confirm_running_build():
    with _patched() as root:
        assert update.confirm_running_build() is False  # встроенный код — подтверждать нечего
        d = root / "build-97"
        d.mkdir(parents=True)
        (d / ".pending").write_text("1")
        os.environ.update(WCL_CODE_BUILD="97", WCL_CODE_PATH=str(d))
        assert update.confirm_running_build() is True
        assert (d / ".confirmed").exists() and not (d / ".pending").exists()
    print("OK подтверждение сборки: метка .confirmed после запуска сервера")


_PROBE = """
import os, sys
sys.path.insert(0, {root!r})
import wcl_boot
wcl_boot.SHELL_ID = {shell!r}
b = wcl_boot.activate(log=lambda m: print('LOG', m))
import wcl_analyzer, wcl_analyzer._build as v
print('RES', b, v.BUILD, 'code' in wcl_analyzer.__file__)
if {fall!r} and b:
    print('FALL', wcl_boot.fall_back('boom', log=lambda m: print('LOG', m)))
    import wcl_analyzer as w2
    print('AFTER', 'code' in w2.__file__, os.environ.get('WCL_CODE_BUILD'))
"""


def _probe(data: Path, fall: bool = False) -> str:
    env = {**os.environ, "WCL_DATA_DIR": str(data)}
    env.pop("WCL_CODE_BUILD", None)
    env.pop("WCL_CODE_PATH", None)
    r = subprocess.run([sys.executable, "-c", _PROBE.format(root=str(ROOT), shell=SHELL, fall=fall)],
                       capture_output=True, text=True, env=env, cwd=str(data))
    assert r.returncode == 0, r.stderr
    return r.stdout


def _install(data: Path, build: int) -> Path:
    root = data / "code"
    d = root / f"build-{build}"
    for name, body in _pkg_files(build).items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(body)
    (root / "current.json").write_text(json.dumps({"build": build, "shell": SHELL, "path": str(d)}), encoding="utf-8")
    (root / "failed.json").unlink(missing_ok=True)
    return d


def _age(p: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(p, (t, t))


def test_boot_pending_confirmed_failed():
    """Новая сборка ждёт подтверждения; не подтверждённая после запуска — сломана, встроенный код."""
    data = Path(tempfile.mkdtemp())
    d = _install(data, 60)
    out = _probe(data)
    assert "RES 60 60 True" in out, out
    assert (d / ".pending").exists() and not (d / ".confirmed").exists()
    # второй запуск сразу же (первый ещё не успел подтвердить) — сборка не считается сломанной
    out = _probe(data)
    assert "RES 60 60 True" in out, out
    # прошлый запуск упал, не дойдя до сервера: откат на встроенный код, сборка в failed.json
    _age(d / ".pending", 120)
    out = _probe(data)
    assert "RES None dev False" in out and "не запустилось" in out, out
    failed = json.loads((data / "code" / "failed.json").read_text(encoding="utf-8"))
    assert "60" in failed["builds"] and not (data / "code" / "current.json").exists(), failed
    # и больше не пробуется, даже если current.json снова на неё указывает
    (data / "code" / "current.json").write_text(json.dumps({"build": 60, "shell": SHELL, "path": str(d)}))
    assert "RES None dev False" in _probe(data)
    # подтверждённая сборка запускается, даже если осталась старая метка .pending
    d = _install(data, 61)
    (d / ".confirmed").write_text("1")
    (d / ".pending").write_text("1")
    _age(d / ".pending", 120)
    assert "RES 61 61 True" in _probe(data)
    print("OK загрузчик: ждёт подтверждения → подтверждена; упавшая до сервера — встроенный код")


def test_boot_fall_back():
    """Скачанный код упал при работе: встроенный код; не подтверждённая сборка помечается сломанной."""
    data = Path(tempfile.mkdtemp())
    d = _install(data, 62)
    out = _probe(data, fall=True)
    assert "FALL True" in out and "AFTER False None" in out, out
    assert "62" in json.loads((data / "code" / "failed.json").read_text(encoding="utf-8"))["builds"]
    # подтверждённая (уже работала) — откат на этот запуск, но сломанной не считается
    d = _install(data, 63)
    (d / ".confirmed").write_text("1")
    out = _probe(data, fall=True)
    assert "FALL True" in out and "AFTER False None" in out, out
    assert not (data / "code" / "failed.json").exists() and (data / "code" / "current.json").exists()
    print("OK загрузчик: ошибка скачанного кода — повтор на встроенной версии")


def test_exe_hash_checked():
    """Новый .exe с неверной контрольной суммой не заменяет программу."""
    good = b"MZ" + b"\0" * 1000
    restarted: list = []
    tmp = Path(tempfile.mkdtemp())
    old = (platform_support.app_mode, update.restart_exe, sys.executable)
    platform_support.app_mode = lambda: "exe"
    update.restart_exe = lambda new=None: restarted.append(new)
    sys.executable = str(tmp / "WCL Analyzer.exe")
    new_exe = tmp / "WCL Analyzer.new.exe"
    try:
        cases = [
            ({"format": 2, "exe_sha256": hashlib.sha256(b"other").hexdigest(), "exe_size": len(good)}, "повреждена"),
            ({"format": 2, "exe_sha256": hashlib.sha256(good).hexdigest(), "exe_size": 10}, "не полностью"),
            ({"format": 2}, "нет контрольной суммы"),
        ]
        for extra, why in cases:
            with _patched({"build": 99, "shell": "new", **extra}, good):
                try:
                    update.apply_full()
                    raise AssertionError(f"принят .exe: {extra}")
                except update.UpdateError as e:
                    assert why in str(e), e
            assert not new_exe.exists() and not restarted
        # верная сумма — программа заменяется
        ok = {"format": 2, "exe_sha256": hashlib.sha256(good).hexdigest(), "exe_size": len(good)}
        with _patched({"build": 99, "shell": "new", **ok}, good):
            assert update.apply_full()["ok"]
        assert restarted == [new_exe] and new_exe.read_bytes() == good
        # старый update.json без суммы — проверить нечем, обновление идёт как раньше
        restarted.clear()
        with _patched({"build": 99, "shell": "new"}, good):
            assert update.apply_full()["ok"] and restarted
    finally:
        platform_support.app_mode, update.restart_exe, sys.executable = old
    print("OK обновление .exe: неверная контрольная сумма или размер — замена отменена")


def test_jsdelivr_code_hash():
    """Код по файлам через jsDelivr сверяется с отпечатком из update.json, если он есть."""
    files = {"/" + k: v for k, v in _pkg_files(98).items()}
    src = Path(tempfile.mkdtemp())
    for name, body in files.items():
        (src / name.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
        (src / name.lstrip("/")).write_bytes(body)
    want = update.code_tree_hash(src / "wcl_analyzer")
    old = update._get

    def fake_get(url, timeout=30):
        if "structure=flat" in url:
            return json.dumps({"files": [{"name": n} for n in files]}).encode()
        return files[url.split("@v1.1.98", 1)[1]]
    update._get = fake_get
    try:
        for code_sha, ok in ((want, True), ("0" * 64, False), (None, True)):
            tmp = Path(tempfile.mkdtemp())
            info = {"build": 98, "shell": SHELL, "tag": "v1.1.98", **({"code_sha256": code_sha} if code_sha else {})}
            try:
                update._download_jsdelivr(info, tmp)
                assert ok, "повреждённый код из jsDelivr принят"
                assert 'BUILD = "98"' in (tmp / "wcl_analyzer" / "_build.py").read_text(encoding="utf-8")
            except ValueError as e:
                assert not ok and "контрольная сумма" in str(e), e
    finally:
        update._get = old
    # отпечаток не зависит от _build.py и __pycache__ (их переписывает сборка или создаёт Python)
    (src / "wcl_analyzer" / "_build.py").write_text('BUILD = "1"\n')
    (src / "wcl_analyzer" / "__pycache__").mkdir()
    (src / "wcl_analyzer" / "__pycache__" / "web.cpython-312.pyc").write_bytes(b"x")
    assert update.code_tree_hash(src / "wcl_analyzer") == want
    print("OK обновление через jsDelivr: код сверяется с отпечатком")


def test_restart_script():
    """Скрипт перезапуска .exe: только латиница, пути — из переменных, не больше ~30 попыток замены."""
    bat = update.RESTART_BAT
    bat.encode("ascii")
    assert "geq 30" in bat and "timeout" not in bat and "%WCL_NEW_EXE%" in bat and "%WCL_OLD_EXE%" in bat
    assert bat.index(":failed") < bat.index(":run") < bat.index('start "" "%WCL_OLD_EXE%"')
    exe, new = Path("C:/Пользователи/Игрок/WCL Analyzer.exe"), Path("C:/Пользователи/Игрок/WCL Analyzer.new.exe")
    os.environ.update(WCL_CODE_BUILD="5", WCL_CODE_PATH="x", _PYI_ARCHIVE_FILE="y")
    try:
        env = update._restart_env(exe, new)
        assert env["WCL_OLD_EXE"] == str(exe) and env["WCL_NEW_EXE"] == str(new) and env["WCL_NO_BROWSER"] == "1"
        assert not {"WCL_CODE_BUILD", "WCL_CODE_PATH", "_PYI_ARCHIVE_FILE"} & set(env)
        assert "WCL_NEW_EXE" not in update._restart_env(exe, None)
    finally:
        for k in ("WCL_CODE_BUILD", "WCL_CODE_PATH", "_PYI_ARCHIVE_FILE"):
            os.environ.pop(k, None)
    print("OK перезапуск .exe: русские пути и ограничение попыток замены")


if __name__ == "__main__":
    test_apply_code_skips_old_and_keeps_running_dir()
    test_zip_escape_rejected()
    test_failed_build_not_retried()
    test_confirm_running_build()
    test_boot_pending_confirmed_failed()
    test_boot_fall_back()
    test_exe_hash_checked()
    test_jsdelivr_code_hash()
    test_restart_script()
    print("Все проверки обновления прошли")

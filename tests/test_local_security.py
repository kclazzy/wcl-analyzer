"""Локальная программа и чужие сайты: страница программы работает, а запросы с других сайтов — нет.

Проверяется через настоящий HTTP:
- подмена адреса (чужой домен, указывающий на 127.0.0.1) — отказ;
- POST с другого сайта (Origin не совпадает) и «простой» POST без JSON (text/plain, без предварительной
  проверки браузера) — отказ, файл не записан;
- страница программы отдаётся с политикой содержимого (выполняется только её собственный скрипт);
- обновление программы во время разбора не запускается.

Запуск: python tests/test_local_security.py
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    c.request(method, path, body if isinstance(body, (bytes, str)) or body is None else json.dumps(body), h)
    r = c.getresponse()
    return r.status, dict(r.headers), r.read()


def main():
    tmp = Path(tempfile.mkdtemp())
    os.environ.update(WCL_DATA_DIR=str(tmp / "data"), WCL_GAME_DATA="bundled")
    os.environ.pop("WCL_PUBLIC", None)
    import wcl_analyzer.web as web
    port = _free_port()
    threading.Thread(target=web.serve, kwargs={"port": port, "open_browser": False, "local_only": True,
                                               "same_port": True}, daemon=True).start()
    for _ in range(100):
        try:
            req(port, "GET", "/api/version")
            break
        except OSError:
            time.sleep(0.1)
    target = tmp / "out"
    save = {"dir": str(target), "name": "evil.bat", "data": "ZWNobyBoaQ=="}

    # своя страница: работает, с политикой содержимого и меткой у встроенного скрипта
    st, h, body = req(port, "GET", "/")
    csp = h.get("Content-Security-Policy", "")
    assert st == 200 and "script-src 'self' 'nonce-" in csp, (st, csp)
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">'.encode() in body

    # подмена DNS: Host — чужой домен
    st, _, _ = req(port, "GET", "/api/version", headers={"Host": f"evil.example:{port}"})
    assert st == 403, st
    # POST с чужого сайта
    st, _, _ = req(port, "POST", "/api/save", save, headers={"Origin": "https://evil.example"})
    assert st == 403, st
    # «простой» POST без JSON (так его может отправить любая страница без проверки браузера)
    st, _, _ = req(port, "POST", "/api/save", json.dumps(save), headers={"Content-Type": "text/plain"})
    assert st == 403, st
    assert not target.exists(), "файл не должен был появиться"
    # со своей страницы — проходит (адрес совпадает)
    st, _, _ = req(port, "POST", "/api/zones", {}, headers={"Origin": f"http://127.0.0.1:{port}"})
    assert st != 403, st

    # «i» у способности без описания в разборе: описание с Wowhead по запросу
    from wcl_analyzer import wowhead
    orig = wowhead.lookup
    wowhead.lookup = lambda ids, save=None, limit=15, timeout=8: {int(i): {"name": "Волна", "desc": "Наносит урон всем."} for i in ids}
    try:
        st, _, b = req(port, "POST", "/api/spell", {"id": 1234}, headers={"Origin": f"http://127.0.0.1:{port}"})
        assert st == 200 and json.loads(b)["desc"] == "Наносит урон всем.", (st, b)
        st, _, b = req(port, "POST", "/api/spell", {"id": 99_000_000}, headers={"Origin": f"http://127.0.0.1:{port}"})
        assert st == 400, (st, b)   # номер вне диапазона способностей
        assert all(web._spell_rate_ok("1.2.3.4") for _ in range(web.SPELL_PER_MIN))
        assert not web._spell_rate_ok("1.2.3.4"), "публичный сервер: не больше SPELL_PER_MIN описаний в минуту с адреса"
    finally:
        wowhead.lookup = orig
    # резервные копии из «Загрузок» — только в Android-приложении
    st, _, _ = req(port, "POST", "/api/android/backups", {}, headers={"Origin": f"http://127.0.0.1:{port}"})
    assert st == 403, st
    # обновление во время разбора — отказ
    web.JOBS["busy"] = {"id": "busy", "state": "running", "progress": 0.1, "log": [], "created": time.time()}
    try:
        st, _, b = req(port, "POST", "/api/update/apply", {"kind": "code"}, headers={"Origin": f"http://127.0.0.1:{port}"})
        assert st == 409 and "разбор" in json.loads(b)["error"], (st, b)
    finally:
        web.JOBS.pop("busy", None)
    print("OK локальная программа: чужие сайты и подмена адреса — отказ, своя страница — с политикой скриптов, "
          "обновление не обрывает разбор")


if __name__ == "__main__":
    main()

"""Публичный сервис через настоящий HTTP: сервер ничего не хранит о пользователях.

Проверяется: ключ только из заголовков, никаких cookie, ничего не пишется на диск,
результат и Excel удаляются после того, как браузер их забрал, эталоны обновляются
по списку, который присылает браузер.

Запуск: python tests/test_web.py
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))


class Browser:
    def __init__(self, port, key=None):
        self.port, self.key, self.set_cookies = port, key, []

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Content-Type": "application/json"}
        if self.key:
            h["X-WCL-Id"], h["X-WCL-Secret"] = self.key
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        self.set_cookies += r.headers.get_all("Set-Cookie") or []
        data = r.read()
        return r.status, (json.loads(data) if r.headers.get("Content-Type", "").startswith("application/json") else data)

    def wait(self, job):
        for _ in range(300):
            st, s = self.req("GET", f"/api/job/{job}")
            if st != 200 or s["state"] != "running":
                return st, s
            time.sleep(0.2)
        raise TimeoutError(job)


def main():
    tmp = Path(tempfile.mkdtemp())
    os.chdir(tmp)
    os.environ.update(WCL_DATA_DIR=str(tmp / "data"), WCL_PUBLIC="1", PORT="8871")

    from test_pipeline import FakeClient
    import wcl_analyzer.web as web
    from wcl_analyzer.api import MemoryCache

    shared = MemoryCache(50)
    clients = {}

    def factory(creds):
        if not creds:
            raise SystemExit(web.NEED_KEY)
        if creds not in clients:
            clients[creds] = FakeClient()
            clients[creds].cache = shared
        return clients[creds]

    def check_key(cid, sec):
        if not cid.startswith("good"):
            raise ValueError("Warcraft Logs не принял ключ: проверьте Client ID и Client Secret.")

    web.CLIENT_FACTORY, web.CHECK_KEY = factory, check_key
    threading.Thread(target=web.serve, kwargs={"port": 8871, "public": True}, daemon=True).start()
    time.sleep(1.0)
    url = "https://www.warcraftlogs.com/reports/MYREPORT0001#source=7"

    anna, boris = Browser(8871), Browser(8871)
    st, s = anna.req("GET", "/api/status")
    assert st == 200 and s["public"] and not s["has_key"] and not s["server_key"]

    st, s = anna.req("POST", "/api/inspect", {"url": url})
    assert st == 400 and s.get("need_key"), s
    assert anna.req("POST", "/api/key", {"client_id": "bad", "client_secret": "x"})[0] == 400
    assert anna.req("POST", "/api/key", {"client_id": "good-anna", "client_secret": "s1"})[0] == 200
    anna.key, boris.key = ("good-anna", "s1"), ("good-boris", "s2")

    # Счётчик лимита в шапке: без rate_limit у клиента — «нет данных», с ним — потрачено, лимит, сброс
    st, s = anna.req("GET", "/api/limit")
    assert st == 200 and s["available"] is False, s
    from test_pipeline import FakeClient as _FC
    _FC.rate_limit = lambda self: {"limitPerHour": 3600, "pointsSpentThisHour": 120.5, "pointsResetIn": 900}
    st, s = anna.req("GET", "/api/limit")
    assert st == 200 and s["available"] and s["limit"] == 3600 and s["spent"] == 120.5 and s["reset_in"] == 900, s
    del _FC.rate_limit

    # Слежение за живым логом: список боёв с боссами и сколько минут отчёт не пополнялся
    st, s = anna.req("POST", "/api/live", {"url": url})
    assert st == 200 and isinstance(s["fights"], list) and s["fights"] and {"id", "name", "kill", "pct"} <= set(s["fights"][0]), s
    assert anna.req("POST", "/api/live", {"url": "not a link"})[0] == 400

    # Обновление программы: на публичном сервере его нет
    st, s = anna.req("GET", "/api/version")
    assert st == 200 and s["updates"] is False and s["build"], s
    assert anna.req("POST", "/api/update/check", {})[0] == 403

    # Анна сравнивает лог; Борис параллельно разбирает рейд
    st, s = anna.req("POST", "/api/analyze", {"url": url, "fight": "1", "actor": "7", "ref": "top10",
                                              "max_age_days": 7})
    job_a = s["job"]
    st, s = boris.req("POST", "/api/analyze", {"mode": "raid", "demo": True})
    job_b = s["job"]
    st, ra = anna.wait(job_a)
    st, rb = boris.wait(job_b)
    assert ra["state"] == "done" and rb["state"] == "done" and rb["result"]["mode"] == "raid"
    ref = ra["result"]["ref"]
    assert ref["cls"] == "Mage" and ref["top_n"] == 10 and ref["collected_at"]

    # Браузер забирает Excel, сообщает «забрал» — сервер сразу удаляет результат
    st, data = anna.req("GET", f"/api/report/{job_a}")
    assert st == 200 and data[:2] == b"PK"
    anna.req("POST", "/api/job/done", {"job": job_a})
    assert anna.req("GET", f"/api/job/{job_a}")[0] == 404
    assert anna.req("GET", f"/api/report/{job_a}")[0] == 404
    assert boris.req("GET", "/api/job/" + "0" * 32)[0] == 404

    # Обновление эталонов: список присылает браузер, сервер возвращает свежие данные
    st, s = anna.req("POST", "/api/refs/refresh", {"refs": [ref]})
    st, s = anna.wait(s["job"])
    assert s["state"] == "done" and s["result"]["count"] == 1
    assert s["result"]["refs"][0]["collected_at"] >= ref["collected_at"]

    # Старые эндпоинты хранения на сервере больше не существуют
    for path in ("/api/refs", "/api/settings", "/api/refs/sync", "/api/adopt"):
        assert anna.req("POST" if path != "/api/refs" else "GET", path, {} if path != "/api/refs" else None)[0] == 404

    # Сервер не ставит cookie и ничего не пишет на диск
    assert not anna.set_cookies and not boris.set_cookies, anna.set_cookies
    written = [p for p in tmp.rglob("*") if p.is_file()]
    assert not written, written
    print("OK веб: сервер без хранения данных — без cookie и файлов, результат удаляется после выдачи, "
          "эталоны обновляются по списку из браузера")


if __name__ == "__main__":
    main()

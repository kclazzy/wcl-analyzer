"""Клиент Warcraft Logs API v2 (GraphQL) с SQLite-кэшем.

Каждый ответ API сохраняется в кэше по sha256 от текста запроса и переменных,
поэтому повторный анализ не тратит очки лимита.
"""
from __future__ import annotations

import hashlib
import os
import json
import re
import sqlite3
import threading
import time
import zlib
from pathlib import Path
from typing import Any

import requests

from .config import API_URL, TOKEN_URL, api_url, site_key, site_url, token_url


class WCLError(RuntimeError):
    pass


class RateLimitError(WCLError):
    """Кончился часовой лимит запросов Warcraft Logs (в тексте всегда есть слово «лимит»)."""


CACHE_MAX_AGE_DAYS = 21    # ответы старше — удаляются при запуске (понадобятся — скачаются заново)
CACHE_MAX_MB = 500         # и не больше 500 МБ: сверх — удаляются самые старые
_Z = b"z1"                 # метка сжатого ответа (старые записи — обычный текст JSON)


class Cache:
    """Общий кэш ответов API. Данные WCL публичные, поэтому кэш общий для всех пользователей.
    Ответы хранятся сжатыми (zlib, в 5–10 раз меньше). Старые и лишние записи чистит prune()."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS api_cache ("
            " key TEXT PRIMARY KEY, query TEXT, variables TEXT,"
            " response TEXT, fetched_at REAL)"
        )
        self.db.commit()

    @staticmethod
    def key(query: str, variables: dict) -> str:
        raw = query.strip() + "\n" + json.dumps(variables, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _pack(response: Any):
        raw = json.dumps(response, separators=(",", ":")).encode("utf-8")
        return sqlite3.Binary(_Z + zlib.compress(raw, 6)) if len(raw) > 1024 else raw.decode("utf-8")

    @staticmethod
    def _unpack(data) -> Any:
        if isinstance(data, (bytes, memoryview)):
            data = bytes(data)
            return json.loads(zlib.decompress(data[len(_Z):]) if data.startswith(_Z) else data)
        return json.loads(data)

    def get(self, key: str, max_age_s: float | None = None) -> Any | None:
        with self.lock:
            row = self.db.execute(
                "SELECT response, fetched_at FROM api_cache WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        if max_age_s is not None and time.time() - row[1] > max_age_s:
            return None
        return self._unpack(row[0])

    def fetched_at(self, key: str) -> float | None:
        with self.lock:
            row = self.db.execute("SELECT fetched_at FROM api_cache WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, query: str, variables: dict, response: Any) -> None:
        with self.lock:
            self._put(key, query, variables, response)

    def _put(self, key: str, query: str, variables: dict, response: Any) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO api_cache VALUES (?, ?, ?, ?, ?)",
            (key, "", json.dumps(variables, sort_keys=True), self._pack(response), time.time()),
        )
        self.db.commit()

    def size_mb(self) -> float:
        try:
            return self.path.stat().st_size / 1048576
        except OSError:
            return 0.0

    def info(self) -> dict:
        with self.lock:
            n = self.db.execute("SELECT COUNT(*) FROM api_cache").fetchone()[0]
        return {"path": str(self.path.resolve()), "size_mb": round(self.size_mb(), 1), "rows": n,
                "max_age_days": CACHE_MAX_AGE_DAYS, "max_mb": CACHE_MAX_MB}

    def prune(self, max_age_days: float = CACHE_MAX_AGE_DAYS, max_mb: float = CACHE_MAX_MB,
              compress_old: bool = True) -> dict:
        """Удаляет ответы старше max_age_days и самые старые сверх max_mb, дожимает старые несжатые записи
        и возвращает место на диске (VACUUM). Свои эталоны и настройки (другие таблицы) не трогает."""
        before = self.size_mb()
        with self.lock:
            cur = self.db.execute("DELETE FROM api_cache WHERE fetched_at < ?", (time.time() - max_age_days * 86400,))
            removed = cur.rowcount or 0
            if compress_old:  # записи прежних версий — текстом и с полным текстом запроса
                rows = self.db.execute("SELECT key, response FROM api_cache WHERE typeof(response) = 'text' "
                                       "AND length(response) > 1024").fetchall()
                for k, resp in rows:
                    self.db.execute("UPDATE api_cache SET response = ?, query = '' WHERE key = ?",
                                    (self._pack(json.loads(resp)), k))
                self.db.execute("UPDATE api_cache SET query = '' WHERE query != ''")
            total = self.db.execute("SELECT COALESCE(SUM(length(response)), 0) FROM api_cache").fetchone()[0]
            limit = max_mb * 1048576
            if total > limit:  # сверх лимита — самые старые
                acc, cut = 0, None
                for k, ln, fa in self.db.execute("SELECT key, length(response), fetched_at FROM api_cache "
                                                 "ORDER BY fetched_at DESC"):
                    acc += ln or 0
                    if acc > limit * 0.9:
                        cut = fa
                        break
                if cut is not None:
                    removed += self.db.execute("DELETE FROM api_cache WHERE fetched_at <= ?", (cut,)).rowcount or 0
            self.db.commit()
            try:
                self.db.execute("VACUUM")
            except sqlite3.OperationalError:
                pass
        return {"removed": removed, "before_mb": round(before, 1), "after_mb": round(self.size_mb(), 1)}

    def clear(self) -> dict:
        """Очистить кэш целиком (эталоны и настройки остаются)."""
        before = self.size_mb()
        with self.lock:
            self.db.execute("DELETE FROM api_cache")
            self.db.commit()
            self.db.execute("VACUUM")
        return {"before_mb": round(before, 1), "after_mb": round(self.size_mb(), 1)}


class MemoryCache:
    """Кэш ответов WCL только в оперативной памяти: на диск ничего не пишется.
    Нужен публичному сервису, чтобы бои топа не скачивались заново для каждого разбора.
    Хранит только публичные данные Warcraft Logs; при перезапуске очищается."""

    path = None

    def __init__(self, max_mb: float = 150, ttl_s: float = 6 * 3600):
        from collections import OrderedDict
        self.lock = threading.Lock()
        self.data: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
        self.size, self.max_bytes, self.ttl = 0, int(max_mb * 1024 * 1024), ttl_s

    key = staticmethod(Cache.key)

    def _drop(self, key: str) -> None:
        item = self.data.pop(key, None)
        if item:
            self.size -= len(item[0])

    def get(self, key: str, max_age_s: float | None = None) -> Any | None:
        with self.lock:
            item = self.data.get(key)
            if not item:
                return None
            age = time.time() - item[1]
            if age > self.ttl:
                self._drop(key)
                return None
            if max_age_s is not None and age > max_age_s:
                return None
            self.data.move_to_end(key)
            return json.loads(item[0])

    def fetched_at(self, key: str) -> float | None:
        with self.lock:
            item = self.data.get(key)
            return item[1] if item else None

    def put(self, key: str, query: str, variables: dict, response: Any) -> None:
        blob = json.dumps(response)
        with self.lock:
            self._drop(key)
            self.data[key] = (blob, time.time())
            self.size += len(blob)
            while self.size > self.max_bytes and self.data:
                self._drop(next(iter(self.data)))


_NOT_RAID = re.compile(r"dungeon|mythic\+|delve|torghast|challenge|arena|battleground|подземел|арена|поле бо|"
                       r"world boss|мировые боссы|\b(?:beta|бета|ptr|test)\b", re.I)


def pick_raid_zones(zones: list[dict], all_expansions: bool = False) -> list[dict]:
    """Рейдовые зоны из worldData.zones: есть боссы, нет сложности М+ (10) и это не подземелья/арены."""
    raids = []
    for z in zones or []:
        diffs = [d for d in z.get("difficulties") or [] if d.get("id") is not None]
        ids = {int(d["id"]) for d in diffs}
        if not z.get("encounters") or 10 in ids or _NOT_RAID.search(z.get("name") or ""):
            continue
        keep = [d for d in diffs if int(d["id"]) in (3, 4, 5)] or [d for d in diffs if int(d["id"]) != 2] or diffs
        raids.append({**z, "difficulties": keep})
    if not raids:
        return []
    exp_id = lambda z: int((z.get("expansion") or {}).get("id") or 0)  # noqa: E731
    if not all_expansions:
        last = max(exp_id(z) for z in raids)
        raids = [z for z in raids if exp_id(z) == last]
    raids.sort(key=lambda z: (-exp_id(z), bool(z.get("frozen")), -int(z["id"])))
    return [{"id": z["id"], "name": z["name"], "frozen": bool(z.get("frozen")),
             "expansion": (z.get("expansion") or {}).get("name"), "expansion_id": exp_id(z),
             "difficulties": [{"id": int(d["id"]), "name": d.get("name"), "sizes": d.get("sizes") or []}
                              for d in z["difficulties"]],
             "encounters": [{"id": e["id"], "name": e["name"]} for e in z.get("encounters") or []]} for z in raids]


def size_kw(size) -> dict:
    """Аргумент size для fight_rankings — только если размер задан (тестовые клиенты его не знают)."""
    try:
        return {"size": int(size)} if size and int(size) > 0 else {}
    except (TypeError, ValueError):
        return {}


class WCLClient:
    def __init__(self, client_id: str, client_secret: str, cache: Cache,
                 verbose: bool = True):
        self.client_id = client_id
        self.client_secret = client_secret
        self.cache = cache
        self.verbose = verbose
        self._token: str | None = None
        self._token_exp = 0.0
        self.requests_made = 0
        self.cache_hits = 0
        self.session = requests.Session()
        # Параллельные запросы: не больше WCL_PARALLEL одновременно (по умолчанию 4)
        self._sem = threading.BoundedSemaphore(max(1, int(os.environ.get("WCL_PARALLEL", "4"))))
        self._token_lock = threading.Lock()
        self._count_lock = threading.Lock()
        self.on_wait = None  # колбэк(секунды): программа ждёт сброса часового лимита API
        # Сколько секунд можно ждать сброса лимита. Для быстрых действий (поиск боя) — 0:
        # сразу сказать пользователю, а не «висеть» до часа.
        self.max_wait_s: float | None = None
        self.site = "www"            # версия игры: www (основная), classic, fresh, sod, vanilla
        self._tokens: dict = {}      # сайт → (токен, когда истекает); общий у копий for_site/with_limits

    @property
    def api_url(self) -> str:
        return API_URL if self.site == "www" else api_url(self.site)

    @property
    def site_url(self) -> str:
        return site_url(self.site)

    def for_site(self, site: str | None) -> "WCLClient":
        """Тот же клиент (ключ, кэш, соединение, лимит параллельных запросов) для другой версии игры:
        у Classic, Season of Discovery и т. п. — свой сайт Warcraft Logs и свой API."""
        import copy
        site = site_key(site)
        if site == self.site:
            return self
        c = copy.copy(self)
        c.site = site
        return c

    # ------------------------------------------------------------------ auth
    def token(self) -> str:
        with self._token_lock:
            return self._token_locked()

    def _token_locked(self) -> str:
        tok, exp = self._tokens.get(self.site, (None, 0.0))
        if self.site == "www" and self._token and self._token_exp > exp:   # токен, заданный напрямую (тесты)
            tok, exp = self._token, self._token_exp
        if tok and time.time() < exp - 60:
            return tok
        r = None
        for url in dict.fromkeys([TOKEN_URL if self.site == "www" else token_url(self.site), TOKEN_URL]):
            r = self.session.post(url, data={"grant_type": "client_credentials"},
                                  auth=(self.client_id, self.client_secret), timeout=30)
            if r.status_code == 200:
                break
        if r is None or r.status_code != 200:
            raise WCLError(f"Не удалось получить токен WCL: {r.status_code if r is not None else '—'} "
                           f"{r.text[:200] if r is not None else ''}")
        data = r.json()
        tok, exp = data["access_token"], time.time() + float(data.get("expires_in", 3600))
        self._tokens[self.site] = (tok, exp)
        if self.site == "www":
            self._token, self._token_exp = tok, exp
        return tok

    def _key(self, query: str, variables: dict) -> str:
        """Ключ кэша: у другой версии игры — свой (те же номера отчётов и боссов значат другое)."""
        return Cache.key(query, variables if self.site == "www" else {**variables, "_site": self.site})

    # ----------------------------------------------------------------- query
    def query(self, query: str, variables: dict | None = None,
              use_cache: bool = True, max_age_s: float | None = None) -> dict:
        variables = variables or {}
        key = self._key(query, variables)
        if use_cache:
            hit = self.cache.get(key, max_age_s)
            if hit is not None:
                with self._count_lock:
                    self.cache_hits += 1
                return hit
        limited = errors_5xx = 0
        for _attempt in range(16):
            with self._sem:   # слот — только на сам запрос: ожидание сброса лимита идёт вне его
                r = self._post(query, variables)
            if r.status_code == 429:
                limited += 1
                wait = self._reset_wait(r)
                # ждать нельзя — сразу сказать; ждать можно — не больше 12 раз подряд (почти сутки сбоев — уже не лимит)
                if (self.max_wait_s is not None and wait > self.max_wait_s) or limited > (3 if self.max_wait_s is not None else 12):
                    raise RateLimitError(
                        "Закончился часовой лимит запросов Warcraft Logs для вашего ключа — разбор остановлен. "
                        f"Сброс примерно через {max(1, round(wait / 60))} мин — тогда нажмите «Продолжить»: "
                        "уже скачанное сохранено и лимит повторно не тратит.")
                self._log(f"Лимит очков исчерпан, жду {wait:.0f} с до сброса…")
                if self.on_wait:
                    try:
                        self.on_wait(wait)
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                errors_5xx += 1
                if errors_5xx > 6:
                    break
                time.sleep(min(30, 2 ** errors_5xx))
                continue
            if r.status_code != 200:
                raise WCLError(f"WCL API вернул {r.status_code}: {r.text[:300]}")
            body = r.json()
            if body.get("errors"):
                msg = "; ".join(e.get("message", "") for e in body["errors"])
                raise WCLError(f"Ошибка GraphQL: {msg}")
            data = body["data"]
            if use_cache:
                self.cache.put(key, query, variables, data)
            return data
        raise WCLError("WCL API не ответил после нескольких попыток")

    def _post(self, query: str, variables: dict):
        r = self.session.post(
            self.api_url,
            json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {self.token()}"},
            timeout=60,
        )
        with self._count_lock:
            self.requests_made += 1
        return r

    def with_limits(self, max_wait_s: float | None, on_wait=None) -> "WCLClient":
        """Тот же клиент (токен, кэш, соединение, общий счётчик параллельных запросов) со своими настройками
        ожидания лимита — для одного разбора. Настройки одного разбора не влияют на другие."""
        import copy
        c = copy.copy(self)
        c.max_wait_s, c.on_wait = max_wait_s, on_wait
        return c

    def _reset_wait(self, r=None) -> float:
        """Сколько ждать сброса лимита: заголовок Retry-After, иначе — сколько осталось по данным WCL."""
        try:
            ra = float((r.headers or {}).get("Retry-After")) if r is not None else None
            if ra and ra > 0:
                return ra + 2
        except (TypeError, ValueError, AttributeError):
            pass
        try:
            info = self.rate_limit()
            return float(info.get("pointsResetIn", 60)) + 5
        except Exception:  # noqa: BLE001
            return 60.0

    def rate_limit(self) -> dict:
        q = "query { rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn } }"
        # Один прямой запрос: мимо семафора (его делают потоки, ждущие сброса внутри своего слота)
        # и без повторов при 429 — иначе при исчерпанном лимите он вызывал бы сам себя бесконечно.
        r = self.session.post(self.api_url, json={"query": q}, headers={"Authorization": f"Bearer {self.token()}"},
                              timeout=30)
        if r.status_code != 200:
            raise WCLError(f"WCL API вернул {r.status_code}")
        body = r.json()
        if body.get("errors") or not body.get("data"):
            raise WCLError("Нет данных о лимите")
        return body["data"]["rateLimitData"]

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    # ------------------------------------------------------------ endpoints
    def zone_encounters(self, zone_id: int) -> dict:
        q = """query($id: Int!) { worldData { zone(id: $id) {
                 id name expansion { name }
                 difficulties { id name }
                 encounters { id name } } } }"""
        return self.query(q, {"id": zone_id})["worldData"]["zone"]

    def report(self, code: str) -> dict:
        q = """query($code: String!) { reportData { report(code: $code) {
                 code title startTime endTime
                 zone { id name }
                 fights(killType: Encounters) {
                   id encounterID name difficulty kill startTime endTime size fightPercentage
                   enemyNPCs { id gameID } phaseTransitions { id startTime } }
                 masterData {
                   actors { id name type subType server petOwner }
                   abilities { gameID name type icon } } } } }"""
        rep = self.query(q, {"code": code})["reportData"]["report"]
        if rep is None:
            raise WCLError(f"Отчёт {code} не найден или закрыт")
        rep["_site_url"] = self.site_url   # ссылки на бои — на сайт той версии игры, откуда отчёт
        return rep

    def report_phases(self, code: str) -> list[dict]:
        """Названия фаз боссов отчёта: [{encounterID, phases: [{id, name, isIntermission}]}].
        Необязательные данные: при любой ошибке — пустой список, разбор идёт без названий фаз."""
        q = """query($code: String!) { reportData { report(code: $code) {
                 phases { encounterID phases { id name isIntermission } } } } }"""
        try:
            return (self.query(q, {"code": code})["reportData"]["report"] or {}).get("phases") or []
        except Exception:  # noqa: BLE001
            return []

    def player_details(self, code: str, fight_id: int, combatant: bool = False) -> dict:
        """Состав боя. combatant=True — ещё и CombatantInfo каждого игрока (таланты, экипировка)."""
        q = """query($code: String!, $fid: [Int]) { reportData { report(code: $code) {
                 playerDetails(fightIDs: $fid%s) } } }""" % (", includeCombatantInfo: true" if combatant else "")
        pd = self.query(q, {"code": code, "fid": [fight_id]})["reportData"]["report"]["playerDetails"]
        # Формат JSON: {"data": {"playerDetails": {...}}} или сразу {...}
        for _ in range(3):
            if isinstance(pd, dict) and "data" in pd:
                pd = pd["data"]
            if isinstance(pd, dict) and "playerDetails" in pd:
                pd = pd["playerDetails"]
        return pd or {}

    def events(self, code: str, fight_id: int, start: float, end: float,
               data_type: str, source_id: int | None = None,
               target_id: int | None = None, hostility: str | None = None,
               include_resources: bool = False, filter_expression: str | None = None) -> list[dict]:
        """Все события с пагинацией по nextPageTimestamp. filter_expression — фильтр WCL
        (например, «ability.id in (62618, 64843)»): сервер отдаёт только нужные события."""
        q = """query($code: String!, $fid: [Int], $start: Float, $end: Float,
                     $dt: EventDataType, $src: Int, $tgt: Int,
                     $host: HostilityType, $res: Boolean, $filter: String) {
                 reportData { report(code: $code) {
                   events(fightIDs: $fid, startTime: $start, endTime: $end,
                          dataType: $dt, sourceID: $src, targetID: $tgt,
                          hostilityType: $host, includeResources: $res,
                          filterExpression: $filter,
                          limit: 10000) { data nextPageTimestamp } } } }"""
        out: list[dict] = []
        cursor = start
        while cursor is not None and cursor < end:
            variables = {"code": code, "fid": [fight_id], "start": cursor, "end": end,
                         "dt": data_type, "src": source_id, "tgt": target_id,
                         "host": hostility or "Friendlies", "res": include_resources}
            if filter_expression:
                variables["filter"] = filter_expression
            page = self.query(q, variables)["reportData"]["report"]["events"]
            out.extend(page.get("data") or [])
            nxt = page.get("nextPageTimestamp")
            cursor = nxt if nxt and nxt > cursor else None
        return out

    def damage_table(self, code: str, fight_id: int, source_id: int) -> dict:
        q = """query($code: String!, $fid: [Int], $src: Int) { reportData {
                 report(code: $code) { table(fightIDs: $fid, dataType: DamageDone,
                                             sourceID: $src) } } }"""
        t = self.query(q, {"code": code, "fid": [fight_id], "src": source_id})
        t = t["reportData"]["report"]["table"]
        return t.get("data", t) if isinstance(t, dict) else {}

    def raid_table(self, code: str, fight_id: int, data_type: str) -> dict:
        """Таблица по всем участникам боя: DamageDone, Healing, DamageTaken…"""
        q = """query($code: String!, $fid: [Int], $dt: TableDataType) { reportData {
                 report(code: $code) { table(fightIDs: $fid, dataType: $dt) } } }"""
        t = self.query(q, {"code": code, "fid": [fight_id], "dt": data_type})
        return t["reportData"]["report"]["table"] or {}

    def report_rankings(self, code: str, fight_id: int) -> dict:
        """Рейтинги игроков в бою (rankPercent — процентиль, bracketPercent — ilvl%). Только для киллов."""
        q = """query($code: String!, $fid: [Int]) { reportData {
                 report(code: $code) { rankings(fightIDs: $fid) } } }"""
        r = self.query(q, {"code": code, "fid": [fight_id]})
        return r["reportData"]["report"]["rankings"] or {}

    def rankings(self, encounter_id: int, class_name: str, spec_name: str,
                 difficulty: int, page: int = 1, force: bool = False,
                 max_age_s: float = 3 * 86400) -> dict:
        """Страница рейтинга. force=True — скачать заново, иначе кэш не старше max_age_s.
        В ответ добавляется _fetched_at — когда рейтинг был скачан."""
        q = """query($enc: Int!, $cls: String, $spec: String, $diff: Int, $page: Int) {
                 worldData { encounter(id: $enc) { id name zone { id name }
                   characterRankings(className: $cls, specName: $spec,
                                     difficulty: $diff, page: $page, metric: dps) } } }"""
        variables = {"enc": encounter_id, "cls": class_name, "spec": spec_name, "diff": difficulty, "page": page}
        enc = self.query(q, variables, max_age_s=0 if force else max_age_s)["worldData"]["encounter"]
        enc = dict(enc or {})
        enc["_fetched_at"] = self.cache.fetched_at(self._key(q, variables)) or time.time()
        return enc


    def points_left(self) -> float | None:
        """Сколько очков API осталось в этом часе (None — не удалось узнать)."""
        try:
            info = self.rate_limit()
            return float(info["limitPerHour"]) - float(info["pointsSpentThisHour"])
        except Exception:  # noqa: BLE001
            return None

    def raid_zones(self, max_age_s: float = 3 * 86400, all_expansions: bool = False) -> list[dict]:
        """Рейды для «Топ прогресса» без лога и плана по составу: [{id, name, frozen, expansion, expansion_id,
        difficulties: [{id, name}], encounters}]. Подземелья М+ (сложность 10) и прочие не-рейды отбрасываются.
        all_expansions=False — только последнее дополнение сайта; True — все, новые дополнения первыми.
        Список — с сайта Warcraft Logs той версии игры, что у клиента (основная, Classic, SoD…)."""
        q = """query { worldData { zones { id name frozen expansion { id name }
                 difficulties { id name sizes } encounters { id name } } } }"""
        zones = (self.query(q, {}, max_age_s=max_age_s).get("worldData") or {}).get("zones") or []
        return pick_raid_zones(zones, all_expansions)

    def fight_rankings(self, encounter_id: int, difficulty: int, metric: str = "speed",
                       page: int = 1, max_age_s: float = 3 * 86400, size: int | None = None) -> list[dict]:
        """Лучшие киллы босса (рейтинг гильдий): [{report: {code, fightID}, guild, duration, …}].
        size — размер рейда (Classic: 10 и 25 игроков — разные рейтинги); None — без отбора."""
        if size:
            q = """query($enc: Int!, $diff: Int, $page: Int, $metric: FightRankingMetricType, $size: Int) {
                     worldData { encounter(id: $enc) {
                       fightRankings(difficulty: $diff, page: $page, metric: $metric, size: $size) } } }"""
            variables = {"enc": encounter_id, "diff": difficulty, "page": page, "metric": metric, "size": int(size)}
        else:
            q = """query($enc: Int!, $diff: Int, $page: Int, $metric: FightRankingMetricType) {
                     worldData { encounter(id: $enc) {
                       fightRankings(difficulty: $diff, page: $page, metric: $metric) } } }"""
            variables = {"enc": encounter_id, "diff": difficulty, "page": page, "metric": metric}
        enc = self.query(q, variables, max_age_s=max_age_s)["worldData"]["encounter"] or {}
        data = enc.get("fightRankings") or {}
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
            data = data["data"]
        return (data.get("rankings") if isinstance(data, dict) else data) or []


    # ------------------------------------------------------- пакетные запросы
    _EVENT_ARGS = {"source_id": "sourceID", "target_id": "targetID"}

    def events_multi(self, code: str, fight_id: int, start: float, end: float,
                     specs: dict[str, dict], tables: dict[str, dict] | None = None) -> dict:
        """Несколько выборок событий (и таблиц) одного боя одним запросом GraphQL.

        specs: {имя: {"data_type": "Casts", "source_id": 7, "target_id": None, "hostility": "Enemies",
                      "include_resources": True, "start": …, "end": …}}
        tables: {имя: {"data_type": "DamageDone", "source_id": 7}}
        Вместо ~10 запросов на игрока — один (плюс догрузка длинных выборок по страницам)."""
        parts = []
        names = list(specs)
        for i, name in enumerate(names):
            p = specs[name]
            fids = f"[{int(p['fight_id'])}]" if p.get("fight_id") is not None else "$fid"
            args = [f"fightIDs: {fids}", f"startTime: {float(p.get('start', start))}",
                    f"endTime: {float(p.get('end', end))}", f"dataType: {p['data_type']}",
                    f"hostilityType: {p.get('hostility') or 'Friendlies'}", "limit: 10000"]
            for key, gql in self._EVENT_ARGS.items():
                if p.get(key) is not None:
                    args.append(f"{gql}: {int(p[key])}")
            if p.get("include_resources"):
                args.append("includeResources: true")
            if p.get("filter_expression"):
                args.append(f"filterExpression: {json.dumps(p['filter_expression'])}")
            parts.append(f"e{i}: events({', '.join(args)}) {{ data nextPageTimestamp }}")
        tnames = list(tables or {})
        for i, name in enumerate(tnames):
            p = tables[name]
            args = ["fightIDs: $fid", f"dataType: {p['data_type']}"]
            if p.get("source_id") is not None:
                args.append(f"sourceID: {int(p['source_id'])}")
            parts.append(f"t{i}: table({', '.join(args)})")
        q = ("query($code: String!, $fid: [Int]) { reportData { report(code: $code) { "
             + " ".join(parts) + " } } }")
        rep = self.query(q, {"code": code, "fid": [fight_id]})["reportData"]["report"] or {}
        out: dict = {}
        for i, name in enumerate(names):
            p = specs[name]
            page = rep.get(f"e{i}") or {}
            data = list(page.get("data") or [])
            nxt = page.get("nextPageTimestamp")
            if nxt:  # длинная выборка: остальные страницы — обычными запросами
                data += self.events(code, int(p.get("fight_id") or fight_id), float(nxt), float(p.get("end", end)), p["data_type"],
                                    source_id=p.get("source_id"), target_id=p.get("target_id"),
                                    hostility=p.get("hostility"), include_resources=bool(p.get("include_resources")),
                                    filter_expression=p.get("filter_expression"))
            out[name] = data
        for i, name in enumerate(tnames):
            t = rep.get(f"t{i}") or {}
            out[name] = t.get("data", t) if isinstance(t, dict) else {}
        return out

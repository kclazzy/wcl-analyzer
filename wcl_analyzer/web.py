"""Веб-интерфейс: python -m wcl_analyzer ui

Все данные пользователя хранятся у него в браузере: ключ Warcraft Logs API, эталоны,
настройки, история разборов и Excel-файлы. Сервер только считает:
получает запрос (ключ — в заголовках X-WCL-Id / X-WCL-Secret), загружает данные из WCL,
возвращает результат и Excel и ничего о пользователе не сохраняет — ни на диске, ни в cookie.

Режимы:
- локальный (по умолчанию) — программа на своём компьютере; кэш публичных данных WCL лежит
  на этом же компьютере; телефон в той же Wi-Fi сети подключается по ссылке-ключу из QR-кода;
- публичный (--public или WCL_PUBLIC=1) — сервис в интернете для любых пользователей;
  кэш публичных данных WCL только в оперативной памяти, на диск сервер ничего не пишет.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import socket
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import settings
from .compare import CompareResult, _fmt_t
from .config import cache_path
from .metrics import CATEGORY_RU
from .names_ru import spec_ru

STATIC = Path(__file__).parent / "web" / "index.html"
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
NEED_KEY = "Нужен ключ Warcraft Logs API: нажмите «Подключить ключ API» вверху страницы."
SERVER = {"port": 8765, "lan": False, "public": False}
MAX_JOBS = int(os.environ.get("WCL_MAX_JOBS", "3"))
MAX_QUEUE = int(os.environ.get("WCL_MAX_QUEUE", "20"))
JOB_SLOTS = threading.BoundedSemaphore(MAX_JOBS)
JOB_TTL_S = 3600            # результат и Excel живут в памяти час, пока браузер их не заберёт
DEFAULT_MAX_AGE_DAYS = 3


# ------------------------------------------------------------ клиент API
_CLIENTS: dict[str, object] = {}
_CLIENTS_LOCK = threading.Lock()
_CACHE = {"obj": None}


def _shared_cache():
    """Публичный сервис — кэш только в памяти; локальная программа — файл на этом компьютере."""
    from .api import Cache, MemoryCache
    with _CLIENTS_LOCK:
        if _CACHE["obj"] is None:
            _CACHE["obj"] = (MemoryCache(float(os.environ.get("WCL_MEMCACHE_MB", "150")))
                             if SERVER["public"] else Cache(cache_path()))
        return _CACHE["obj"]


def env_key() -> tuple[str, str] | None:
    """Ключ из .env — только для локальной программы (совместимость и командная строка)."""
    if SERVER["public"]:
        return None
    from .config import load_env
    load_env()
    cid, sec = os.environ.get("WCL_CLIENT_ID"), os.environ.get("WCL_CLIENT_SECRET")
    return (cid, sec) if cid and sec else None


def default_client_factory(creds: tuple[str, str] | None):
    """Клиент WCL для ключа из запроса. Клиенты живут в памяти, чтобы не запрашивать токен
    каждый раз; ключ по хэшу не восстановить, на диск он не попадает."""
    import hashlib
    from .api import WCLClient
    creds = creds or env_key()
    if not creds:
        raise SystemExit(NEED_KEY)
    k = hashlib.sha256(f"{creds[0]}:{creds[1]}".encode()).hexdigest()
    with _CLIENTS_LOCK:
        if k not in _CLIENTS:
            _CLIENTS[k] = WCLClient(creds[0], creds[1], None, verbose=False)
        client = _CLIENTS[k]
    client.cache = _shared_cache()
    return client


CLIENT_FACTORY = default_client_factory  # тесты подменяют на фейковый клиент


def check_key(client_id: str, secret: str) -> None:
    """Проверяет ключ получением токена. Ключ нигде не сохраняется."""
    import requests
    from .config import TOKEN_URL
    if not client_id or not secret:
        raise ValueError("Заполните Client ID и Client Secret.")
    try:
        r = requests.post(TOKEN_URL, data={"grant_type": "client_credentials"},
                          auth=(client_id, secret), timeout=20)
    except requests.RequestException as e:
        raise ValueError(f"Не удалось связаться с Warcraft Logs: {e}") from e
    if r.status_code != 200:
        raise ValueError("Warcraft Logs не принял ключ: проверьте Client ID и Client Secret.")


CHECK_KEY = check_key  # тесты подменяют


# ------------------------------------------------------------- задачи
def _cleanup() -> None:
    now = time.time()
    with JOBS_LOCK:
        for jid in [j for j, job in JOBS.items() if job["state"] != "running" and now - job["created"] > JOB_TTL_S]:
            JOBS.pop(jid, None)


def start_job(creds, params: dict) -> str:
    _cleanup()
    with JOBS_LOCK:
        if sum(1 for j in JOBS.values() if j["state"] == "running") >= MAX_JOBS + MAX_QUEUE:
            raise ValueError("Сервер сейчас загружен — попробуйте через пару минут.")
        jid = uuid.uuid4().hex  # 128 бит: знает только браузер, запустивший разбор
        job = {"id": jid, "state": "running", "progress": 0.02, "log": [], "created": time.time()}
        JOBS[jid] = job
    threading.Thread(target=_run_job, args=(job, params, creds), daemon=True).start()
    return jid


def _run_job(job: dict, params: dict, creds) -> None:
    def log(msg: str) -> None:
        job["log"].append(msg)
        m = re.search(r"\[(\d+)/(\d+)\]", msg)
        if m:
            job["progress"] = 0.15 + 0.75 * int(m.group(1)) / int(m.group(2))

    if not JOB_SLOTS.acquire(blocking=False):
        log("Сервер занят другими разборами — ваш начнётся, как только освободится место…")
        JOB_SLOTS.acquire()
    try:
        mode = params.get("mode")
        if mode == "raid":
            _run_raid(job, params, creds, log)
        elif mode == "raidrot":
            _run_raid_rotation(job, params, creds, log)
        elif mode == "refresh":
            _run_refresh(job, params, creds, log)
        else:
            _run_player(job, params, creds, log)
        job["progress"], job["state"] = 1.0, "done"
    except SystemExit:
        job["state"], job["error"], job["need_key"] = "error", NEED_KEY, True
    except Exception as e:  # noqa: BLE001
        job["state"], job["error"] = "error", _friendly(e)
        job["trace"] = traceback.format_exc()
    finally:
        JOB_SLOTS.release()


def _client(creds, job: dict, log):
    """Клиент WCL для задачи: если кончится часовой лимит API, браузер увидит обратный отсчёт."""
    client = CLIENT_FACTORY(creds)

    def on_wait(seconds: float) -> None:
        job["wait_until"] = time.time() + seconds
        log(f"Закончился часовой лимит запросов Warcraft Logs. Жду сброса: {max(1, round(seconds / 60))} мин. "
            "Не закрывайте страницу: разбор продолжится сам.")

    try:
        client.on_wait = on_wait
    except AttributeError:
        pass
    return client


def _excel_bytes(job: dict, name: str, writer, data, key: str = "xlsx") -> None:
    """Excel собирается во временной папке и сразу удаляется; файл живёт только в памяти задачи."""
    fname = re.sub(r"[^\w\-]+", "_", f"{name}_{time.strftime('%Y%m%d_%H%M%S')}") + ".xlsx"
    with tempfile.TemporaryDirectory() as tmp:
        path = writer(data, Path(tmp) / fname)
        job[key] = Path(path).read_bytes()
    job[key + "_name"] = fname


def _max_age_s(params: dict) -> float:
    try:
        days = float(params.get("max_age_days") or DEFAULT_MAX_AGE_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_MAX_AGE_DAYS
    return max(1.0, min(30.0, days)) * 86400


MYTHIC = 5
# Размер эталона: топ-1, топ-3 или топ-10 (по умолчанию)
REF_SIZES = {"top1": 1, "top3": 3, "top10": 10}


def _run_player(job: dict, params: dict, creds, log) -> None:
    """Разбор игрока: эталон — топ той же сложности, что и ваш бой; если бой не эпохальный —
    дополнительно эпохальный топ (переключатель в шапке результата)."""
    from .collect import collect_reference, load_my_log

    client = None
    ref_meta: dict = {}
    if params.get("demo"):
        from .demo import demo_logs
        log("Генерирую демо-логи: 25 игроков топа и ваш бой…")
        tops, me = demo_logs()
        label = "топ-25 (демо)"
        job["progress"] = 0.8
    else:
        client = _client(creds, job, log)
        log("Загружаю ваш лог…")
        me = load_my_log(client, params["url"], params.get("fight"),
                         actor_id=int(params["actor"]) if params.get("actor") not in (None, "") else None)
        log(f"{me.name}: {me.spec} {me.cls}, {me.encounter_name} ({me.difficulty_name}), "
            f"{_fmt_t(me.duration)}, {me.dps:,.0f} DPS")
        job["progress"] = 0.12
        if params.get("against"):
            log("Загружаю лог для сравнения…")
            other = load_my_log(client, params["against"])
            tops, label = [other], f"Игрок {other.name}"
        else:
            tops, label = _player_ref(client, me, me.difficulty, params, log, ref_meta)
    main = _player_result(job, params, client, me, tops, label, ref_meta, me.difficulty, log, alt=False)

    want_mythic = params.get("mythic") is not False and str(params.get("mythic")).lower() not in ("0", "false")
    if (client is not None and not params.get("against") and want_mythic
            and me.difficulty and int(me.difficulty) != MYTHIC):
        log("Эталон 2 — эпохальный топ: сравнение с самой высокой сложностью…")
        meta2: dict = {}
        try:
            tops2, label2 = _player_ref(client, me, MYTHIC, params, log, meta2)
            main["alt"] = _player_result(job, params, client, me, tops2, label2, meta2, MYTHIC, log, alt=True)
        except Exception as e:  # noqa: BLE001 — второй эталон не обязателен
            log(f"Эпохальный эталон не собран: {e}")
    job["result"] = main
    log("Готово.")


def _player_ref(client, me, difficulty: int, params: dict, log, meta: dict):
    from .collect import collect_reference
    from .config import DIFFICULTY_NAMES
    n = REF_SIZES.get(params.get("ref") or "top10", 10)
    log(f"Собираю эталон: топ-{n} {spec_ru(me.cls, me.spec)} на этом боссе, сложность — "
        f"{DIFFICULTY_NAMES.get(int(difficulty or 0), difficulty)}. Первый раз это занимает несколько минут, дальше быстрее.")
    if params.get("refresh"):
        log("Обновляю рейтинг лучших логов…")
    tops, label = collect_reference(client, me.encounter_id, me.cls, me.spec, difficulty,
                                    top_n=n, duration=me.duration, log=log,
                                    force=bool(params.get("refresh")), meta=meta,
                                    max_age_s=_max_age_s(params), save=not SERVER["public"])
    return tops, label


def _player_result(job, params, client, me, tops, label, ref_meta, top_diff, log, alt: bool) -> dict:
    from .compare import compare
    from .config import DIFFICULTY_NAMES
    from .excel_report import write_compare_workbook
    from .metrics import load_overrides
    from .reference import build_reference
    from .talents import compare_talents, demo_tree_data, load_tree_data
    if client is not None:
        from .raid_rotation import report_points
        report_points(client, log)
    log("Считаю эталон и сравниваю…" + (" (эпохальный топ)" if alt else ""))
    ref = build_reference(tops, load_overrides(_spell_meta_path()), label=label, me=me)
    r = compare(me, ref)
    try:  # по всем скачанным логам топа, а не только по отобранным с похожим билдом
        data = demo_tree_data() if params.get("demo") else load_tree_data(log, save=not SERVER["public"])
        r.talents = compare_talents(me, tops, data=data, log=log)
    except Exception as e:  # noqa: BLE001 — сравнение талантов не обязательно
        log(f"Сравнение талантов недоступно: {e}")
        r.talents = None
    diff_name = DIFFICULTY_NAMES.get(int(top_diff or 0), "")
    suffix = "_эпохальный_топ" if alt else ""
    _excel_bytes(job, f"{me.name}_{me.encounter_name}{suffix}", write_compare_workbook, r,
                 key="xlsx_alt" if alt else "xlsx")
    result = to_json(r, job["id"])
    if alt:
        result["excel"] = f"/api/report/{job['id']}/alt"
    result["info"]["ref_difficulty"] = diff_name
    if ref_meta:
        result["ref"] = {k: ref_meta.get(k) for k in REF_FIELDS}
        result["info"]["ref_collected_at"] = ref_meta["collected_at"]
        result["info"]["ref_stale"] = time.time() - ref_meta["collected_at"] > _max_age_s(params)
    result["params"] = {k: params.get(k) for k in ("url", "fight", "actor", "ref")}
    result["talents"] = r.talents
    return result


def _avoidable_list() -> list[str]:
    """Список избегаемых механик из spell_meta.json: "_avoidable": [id или название, ...]."""
    import json
    try:
        data = json.loads(Path(_spell_meta_path()).read_text(encoding="utf-8"))
        return [str(x) for x in data.get("_avoidable", [])]
    except (OSError, ValueError, AttributeError):
        return []


def _spell_meta_path() -> str:
    """spell_meta.json рядом с программой или в каталоге данных (для .exe и Android)."""
    from .config import data_dir
    for p in (Path("spell_meta.json"), data_dir() / "spell_meta.json"):
        if p.exists():
            return str(p)
    return "spell_meta.json"


REF_FIELDS = ("encounter_id", "boss", "cls", "spec", "difficulty", "top_n", "duration", "label", "n_logs",
              "collected_at")


def _run_refresh(job: dict, params: dict, creds, log) -> None:
    """Обновляет эталоны, присланные браузером: свежий рейтинг и бои новых игроков топа."""
    from .collect import collect_reference
    client = _client(creds, job, log)
    refs = []
    for r in (params.get("refs") or [])[:30]:
        try:
            refs.append({"encounter_id": int(r["encounter_id"]), "cls": str(r["cls"])[:40],
                         "spec": str(r["spec"])[:40], "difficulty": int(r["difficulty"]),
                         "top_n": max(1, min(50, int(r["top_n"]))),
                         "duration": float(r["duration"]) if r.get("duration") else None,
                         "boss": str(r.get("boss", ""))[:80]})
        except (KeyError, TypeError, ValueError):
            continue
    out = []
    for i, r in enumerate(refs):
        log(f"[{i}/{len(refs)}] {r['boss']}: {spec_ru(r['cls'], r['spec'])}, топ-{r['top_n']}…")
        meta: dict = {}
        collect_reference(client, r["encounter_id"], r["cls"], r["spec"], r["difficulty"], top_n=r["top_n"],
                          duration=r["duration"], force=True, log=lambda m: log("  " + m.strip()),
                          meta=meta, max_age_s=_max_age_s(params), save=not SERVER["public"])
        out.append({k: meta.get(k) for k in REF_FIELDS})
    job["result"] = {"mode": "refresh", "count": len(out), "refs": out}
    log(f"Готово: обновлено эталонов — {len(out)}.")


def _run_raid(job: dict, params: dict, creds, log) -> None:
    from .excel_raid import write_raid_workbook
    from .raid import run_raid

    def step(msg: str) -> None:
        log(msg)
        job["progress"] = min(0.5, job["progress"] + 0.07)

    avoidable = set(_avoidable_list())
    if params.get("demo"):
        from .raid_demo import DEMO_AVOIDABLE, DEMO_URL, FakeRaidClient
        step("Генерирую демо-рейд: 20 игроков, 6 пуллов…")
        client, url = FakeRaidClient(), DEMO_URL
        avoidable |= {str(x) for x in DEMO_AVOIDABLE}
    else:
        client, url = _client(creds, job, log), params["url"]
    R = run_raid(client, url, params.get("fight"), log=step, avoidable=avoidable,
                 talent_data=[] if params.get("demo") else None, save_talents=not SERVER["public"],
                 mythic=params.get("mythic") is not False,
                 progress=lambda x: job.__setitem__("progress", max(job["progress"], min(0.97, x))))
    _excel_bytes(job, f"Рейд_{R['info']['boss']}_пулл{R['summary']['pull_n']}", write_raid_workbook, R)
    job["result"] = {**R, "excel": f"/api/report/{job['id']}", "source_url": url}
    log("Готово.")


def _run_raid_rotation(job: dict, params: dict, creds, log) -> None:
    """Каждый DPS боя против топа своего спека; эталон собирается один раз на спек."""
    from .excel_report import write_raid_rotation_workbook
    from .metrics import load_overrides
    from .raid_rotation import run_demo, run_raid_rotation

    def progress(x: float) -> None:
        job["progress"] = max(job["progress"], min(0.95, x))

    n = REF_SIZES.get(params.get("ref") or "top10", 10)
    log(f"Эталон для каждого спека: топ-{n} (размер — в списке «Сравнить с»).")
    if params.get("demo"):
        R = run_demo(25, log=log, progress=progress)
    else:
        R = run_raid_rotation(_client(creds, job, log), params["url"], params.get("fight"), top_n=n, log=log,
                              progress=progress, overrides=load_overrides(_spell_meta_path()),
                              max_age_s=_max_age_s(params), save=not SERVER["public"], pick=params.get("pick"),
                              mythic=params.get("mythic") is not False)
    I = R["info"]
    _excel_bytes(job, f"Ротация_рейда_{I['boss']}", write_raid_rotation_workbook, R)
    players = []
    for r in R["rows"]:
        row = {k: (_num(v, 4) if isinstance(v, float) else v) for k, v in r.items() if k not in ("result", "actions")}
        if "result" in r:
            row["actions"] = r["actions"]
            row["detail"] = to_json(r["result"], job["id"])
        players.append(row)
    job["result"] = {"mode": "raidrot", "info": I, "brief": R["brief"], "players": players,
                     "skipped": [p.get("name") for p in R["skipped"]], "not_picked": R.get("not_picked", []),
                     "pick": R.get("pick"),
                     "excel": f"/api/report/{job['id']}", "source_url": params.get("url")}
    log("Готово.")


def _page() -> bytes:
    """Страница интерфейса с русскими названиями классов и спеков из names_ru.py (один словарь на всё)."""
    from .names_ru import CLASSES, SPECS
    html = STATIC.read_text(encoding="utf-8")
    html = html.replace("/*CLASS_RU*/{}", json.dumps(CLASSES, ensure_ascii=False))
    html = html.replace("/*SPEC_RU*/{}", json.dumps({f"{c}|{s}": v for (c, s), v in SPECS.items()}, ensure_ascii=False))
    return html.encode("utf-8")


def _friendly(e: Exception) -> str:
    msg = str(e)
    if "ConnectionError" in type(e).__name__ or "Max retries" in msg:
        return "Нет связи с Warcraft Logs. Проверьте интернет и попробуйте ещё раз."
    if isinstance(e, (KeyError, IndexError, TypeError)):
        # Не голое «'id'», а понятное сообщение: в данных Warcraft Logs нет ожидаемого поля
        import traceback
        tb = traceback.extract_tb(e.__traceback__)
        where = f"{Path(tb[-1].filename).stem}:{tb[-1].lineno}" if tb else "?"
        return (f"Неожиданный формат данных ({type(e).__name__}: {msg}, место: {where}). "
                "Пришлите это сообщение разработчику — по нему видно, что поправить.")
    return msg or type(e).__name__


# ------------------------------------------------------- сериализация
def _num(x, nd=3):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return x
    return None if math.isnan(v) or math.isinf(v) else round(v, nd)


def _finding(f) -> dict:
    return {"section": f.section, "title": f.title, "my": _num(f.my), "ref": _num(f.ref), "unit": f.unit,
            "diff": _num(f.diff), "impact": _num(f.impact, 4), "level": f.impact_level, "note": f.note,
            "focus": f.focus, "key": f.key, "noise": f.noise, "context": f.context}


def to_json(r: CompareResult, job_id: str) -> dict:
    from .guide import guide_sections
    me, ref, mm, agg = r.me, r.ref, r.mm, r.ref.agg
    name = ref.name_of
    k = me.duration / 60
    my_ng = mm["ngrams"][3]
    my_total = sum(my_ng.values()) or 1
    g, gr = mm["gcd"], agg["gcd"]
    return {
        "mode": "player",
        "info": {"name": me.name, "cls": me.cls, "spec": me.spec, "boss": me.encounter_name,
                 "difficulty": me.difficulty_name, "duration": _fmt_t(me.duration), "kill": me.kill,
                 "url": me.url, "ref_label": ref.label, "ref_n": ref.n, "dps": _num(me.dps, 0),
                 "ref_dps": _num(agg["dps"]["median"], 0), "demo": me.report_code.startswith("DEMO"),
                 "main_cd": name(ref.main_cd) if ref.main_cd else None,
                 # проверка сложности: сколько логов топа той же сложности, что и ваш бой
                 "ref_same_diff": sum(1 for x in ref.logs if x.difficulty == me.difficulty)},
        "excel": f"/api/report/{job_id}",
        "brief": {**r.brief, "gap": _num(r.brief.get("gap"), 4), "explained": _num(r.brief.get("explained"), 4)},
        "progress_keys": {k: {"title": v["title"], "impact": _num(v["impact"], 4)} for k, v in r.progress_keys.items()},
        "build_note": ref.build_note,
        "boss_debuffs": [{kk: _num(v, 4) if isinstance(v, float) else v for kk, v in x.items() if kk != "id"}
                         for x in r.tables.get("boss_debuffs", [])],
        "externals": [{kk: _num(v, 2) if isinstance(v, float) else v for kk, v in x.items() if kk != "id"}
                      for x in r.tables.get("externals", [])],
        "priority": [{kk: _num(v, 3) if isinstance(v, float) else v for kk, v in x.items()}
                     for x in r.tables.get("priority", [])],
        "opener": ({**r.tables["opener"], "dist": _num(r.tables["opener"]["dist"], 3),
                    "ref_dist": _num(r.tables["opener"]["ref_dist"], 3)} if r.tables.get("opener") else None),
        "reliability": r.reliability,
        "summary": [{"metric": x["metric"], "my": _num(x["my"], 4), "ref": _num(x["ref"], 4), "fmt": x["fmt"]}
                    for x in r.summary],
        "top5": [_finding(f) for f in r.top5],
        "findings": [_finding(f) for f in sorted(r.findings, key=lambda f: (f.noise or f.context, -(f.impact or 0), -f.severity))],
        "plan": r.plan,
        "gap_total": _num(r.gap[0]["total"], 0) if r.gap else 0,
        "gap": [{"factor": x["factor"], "dps": _num(x["dps"], 0), "status": x["status"]} for x in r.gap],
        "casts": [{"name": x["name"], "cat": x["cat"], "my": x["my"], "ref": _num((x["ref_cpm"] or 0) * k, 1),
                   "p25": _num((agg["abilities"][x["id"]]["cpm"]["p25"] or 0) * k, 1),
                   "p75": _num((agg["abilities"][x["id"]]["cpm"]["p75"] or 0) * k, 1),
                   "share": _num(x["share"])} for x in r.tables["casts"]],
        "ngrams": [{"seq": " → ".join(name(a) for a in n["seq"]), "share": _num(n["share"], 4),
                    "players": _num(n["player_share"]), "my": _num(my_ng.get(n["seq"], 0) / my_total, 4)}
                   for n in agg["ngrams"][3]],
        "cooldowns": [{**{kk: _num(v, 1) if isinstance(v, float) else v for kk, v in x.items()}}
                      for x in r.tables["cooldowns"]],
        "cd_timing": [{"name": x["name"], "k": x["k"], "my": _num(x["my"], 1), "ref": _num(x["ref"], 1),
                       "p25": _num(x["p25"], 1), "p75": _num(x["p75"], 1),
                       "diff": _num(x["my"] - x["ref"], 1) if x["my"] is not None and x["ref"] is not None else None,
                       "confidence": _num(x["confidence"])} for x in r.tables["cd_timing"]],
        "burst": [{"window": x["window"], "name": x["name"], "ref_t0": _num(x["ref_t0"], 1),
                   "my_t0": _num(x["my_t0"], 1), "ref_offset": _num(x["ref_offset"], 1),
                   "my_offset": _num(x["my_offset"], 1), "share": _num(x["share"])} for x in r.tables["burst"]],
        "defensives": [{kk: _num(v, 1) if isinstance(v, float) else v for kk, v in x.items()}
                       for x in r.tables["defensives"]],
        "deaths": [{"t": _fmt_t(d["t"]), "sources": [[n, _num(v, 0)] for n, v in d["sources"]],
                    "defensives": d["defensives"], "ref_share": _num(d["ref_def_share"])} for d in r.tables["deaths"]],
        "uptime": [{kk: _num(v, 4) if isinstance(v, float) else v for kk, v in x.items()} for x in r.tables["uptime"]],
        "resource_name": mm["resource"]["name"] if mm["resource"] else None,
        "resource": [{kk: _num(v, 4) if isinstance(v, float) else v for kk, v in x.items()} for x in r.tables["resource"]],
        "procs": [{kk: _num(v, 2) if isinstance(v, float) else v for kk, v in x.items()} for x in r.tables["procs"]],
        "gcd": {"my": {"gcd": _num(g["gcd"], 2), "idle": _num(g["idle_per_action"]), "share": _num(g["idle_share"], 4),
                       "total": _num(g["idle_total"], 1)},
                "ref": {"gcd": _num(gr["gcd"]["median"], 2), "idle": _num(gr["idle_per_action"]["median"]),
                        "share": _num(gr["idle_share"]["median"], 4),
                        "total": _num((gr["idle_share"]["median"] or 0) * me.duration, 1)},
                "by30_my": [_num(v, 2) for v in g["idle_by_30s"]],
                "by30_ref": [_num(v, 2) for v in gr["idle_by_30s"]],
                "worst": [{"from": _fmt_t(x["from"]), "to": _fmt_t(x["to"]), "idle": _num(x["idle"], 1),
                           "prev": x["prev_name"], "next": x["next_name"], "ref": _num(x["ref_bucket_idle"], 2)}
                          for x in r.tables["gcd_worst"]]},
        "timeline": [{"lane": e["lane"], "t": _num(e["t"], 1), "cat": e["cat"], "name": e["name"], "k": e["k"],
                      "delta": _num(e.get("delta"), 1)} for e in r.timeline],
        "duration_s": _num(max(me.duration, agg["duration"]["median"] or 0), 1),
        "dps_5s": [_num(v, 0) for v in (mm.get("dps_5s") or [])],
        "mechanics": [{"name": x["name"], "k": x["k"], "ref": _num(x["ref"], 1), "my": _num(x["my"], 1),
                       "key": x["key"]} for x in r.tables["mechanics"]],
        "guide": [{"title": s["title"], "rows": [[a, b, c, _num(d)] for a, b, c, d in s["rows"]]}
                  for s in guide_sections(ref)],
        "categories": CATEGORY_RU,
    }


# ---------------------------------------------------------------- HTTP
WEB = STATIC.parent
STATIC_FILES = {
    "/static/chart.umd.min.js": ("chart.umd.min.js", "application/javascript; charset=utf-8"),
    "/static/qrcode.js": ("qrcode.js", "application/javascript; charset=utf-8"),
    "/static/icon-192.png": ("icon-192.png", "image/png"),
    "/static/icon-512.png": ("icon-512.png", "image/png"),
    "/apple-touch-icon.png": ("icon-180.png", "image/png"),
    "/favicon.ico": ("icon-192.png", "image/png"),
}
MANIFEST = {
    "name": "Разбор лога Warcraft Logs", "short_name": "Разбор WCL", "lang": "ru",
    "start_url": "/", "display": "standalone", "background_color": "#EEF1F4", "theme_color": "#16213A",
    "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}],
}
DENIED_PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Нет доступа</title>
<body style="font:16px/1.5 system-ui,sans-serif;background:#EEF1F4;color:#16213A;margin:0;padding:32px 20px">
<div style="max-width:460px;margin:0 auto;background:#fff;border-radius:14px;padding:24px;border:1px solid #D5DBE4">
<h1 style="font-size:22px;margin:0 0 10px">Нужен ключ доступа</h1>
<p>Откройте программу на компьютере, нажмите «На другом устройстве» и отсканируйте QR-код камерой телефона.</p>
<p style="color:#5C6781">Если QR-код уже сканировали, а доступ пропал — ключ могли сменить или доступ с телефона выключен.</p>
</div></body></html>"""
MAX_BODY = 32 * 1024 * 1024  # резервная копия с историей разборов может весить несколько МБ


def lan_ip() -> str | None:
    """IP компьютера в локальной сети (пакеты не отправляются)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "WCLAnalyzer/3.0"

    def log_message(self, fmt, *args):  # сервер не ведёт журнал запросов
        pass

    # ------------------------------------------------------ доступ
    def _is_local(self) -> bool:
        ip = self.client_address[0]
        return ip in ("127.0.0.1", "::1") or ip.startswith(("::ffff:127.", "127."))

    def _cookie(self, name: str) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def _creds(self) -> tuple[str, str] | None:
        cid = (self.headers.get("X-WCL-Id") or "").strip()
        sec = (self.headers.get("X-WCL-Secret") or "").strip()
        return (cid, sec) if cid and sec else None

    def _allowed(self) -> bool:
        """Публичный сервис открыт всем (данные у каждого в своём браузере).
        Локальная программа: этот компьютер — всегда, телефон — по ссылке-ключу из QR-кода."""
        if SERVER["public"] or self._is_local():
            return True
        st = settings.load()
        if not st["lan"]:
            self._deny()
            return False
        q = parse_qs(urlparse(self.path).query)
        if q.get("k", [None])[0] == st["token"]:
            self.send_response(302)
            self.send_header("Set-Cookie", f"wclk={st['token']}; Path=/; Max-Age=31536000; SameSite=Lax; HttpOnly")
            self.send_header("Location", urlparse(self.path).path or "/")
            self.end_headers()
            return False
        if self._cookie("wclk") == st["token"]:
            return True
        self._deny()
        return False

    def _deny(self):
        if urlparse(self.path).path.startswith("/api/"):
            return self._json({"error": "Нет доступа: отсканируйте QR-код в программе на компьютере."}, 403)
        self._file(DENIED_PAGE.encode("utf-8"), "text/html; charset=utf-8", status=403)

    # ------------------------------------------------------ ответы
    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._file(body, "application/json; charset=utf-8", status=status)

    def _file(self, data: bytes, ctype: str, cache: str = "no-store", extra: dict | None = None, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("Слишком большой запрос")
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    # ------------------------------------------------------ GET
    def do_GET(self):  # noqa: N802
        if not self._allowed():
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._file(_page(), "text/html; charset=utf-8")
        elif path in STATIC_FILES:
            name, ctype = STATIC_FILES[path]
            self._file((WEB / name).read_bytes(), ctype, "max-age=86400")
        elif path == "/manifest.webmanifest":
            self._file(json.dumps(MANIFEST, ensure_ascii=False).encode("utf-8"), "application/manifest+json")
        elif path == "/api/limit":
            # Часовой лимит Warcraft Logs для ключа из заголовков: сколько очков потрачено и когда сброс.
            # Запрос rateLimitData лёгкий; страница спрашивает раз в минуту и после каждого разбора.
            try:
                client = CLIENT_FACTORY(self._creds())
                if not hasattr(client, "rate_limit"):
                    return self._json({"available": False})
                info = client.rate_limit()
                self._json({"available": True, "limit": info.get("limitPerHour"),
                            "spent": info.get("pointsSpentThisHour"), "reset_in": info.get("pointsResetIn")})
            except SystemExit:
                self._json({"available": False, "need_key": True})
            except Exception as e:  # noqa: BLE001
                self._json({"available": False, "error": _friendly(e)})
        elif path == "/api/status":
            from .platform_support import app_mode
            self._json({"has_key": bool(self._creds() or env_key()), "server_key": bool(env_key()),
                        "public": SERVER["public"], "local": self._is_local(), "app": app_mode()})
        elif path == "/api/phone":
            # Только локальная программа и только на самом компьютере: адрес для телефона в Wi-Fi сети
            if SERVER["public"] or not self._is_local():
                return self._json({"error": "Доступно только на компьютере с программой"}, 403)
            st, ip = settings.load(), lan_ip()
            base = f"http://{ip}:{SERVER['port']}/?k={st['token']}" if ip and st["lan"] and SERVER["lan"] else None
            self._json({"setting": st["lan"], "bound_lan": SERVER["lan"], "ip": ip, "base": base})
        elif path.startswith("/api/job/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if not job:
                return self._json({"error": "Разбор не найден: результаты хранятся на сервере не дольше часа"}, 404)
            since = int(self.headers.get("X-Log-Since") or 0)
            self._json({"state": job["state"], "progress": job["progress"], "log": job["log"][since:],
                        "wait_left": max(0.0, job.get("wait_until", 0) - time.time()),
                        "log_total": len(job["log"]), "error": job.get("error"), "need_key": job.get("need_key", False),
                        "result": job.get("result") if job["state"] == "done" else None})
        elif path.startswith("/api/report/"):
            parts = path[len("/api/report/"):].split("/")
            job, key = JOBS.get(parts[0]), "xlsx_alt" if parts[1:] == ["alt"] else "xlsx"
            if not job or not job.get(key):
                return self._json({"error": "Отчёт уже удалён с сервера — откройте его из истории разборов"}, 404)
            self._file(job[key], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       extra={"Content-Disposition": f"attachment; filename*=UTF-8''{_quote(job[key + '_name'])}"})
        else:
            self._json({"error": "Не найдено"}, 404)

    # ------------------------------------------------------ POST
    def do_POST(self):  # noqa: N802
        if not self._allowed():
            return
        path = urlparse(self.path).path
        try:
            body = self._body()
            creds = self._creds()
            if path == "/api/key":
                CHECK_KEY(str(body.get("client_id", "")).strip(), str(body.get("client_secret", "")).strip())
                return self._json({"ok": True})
            if path == "/api/inspect":
                from .collect import inspect_report
                cl = CLIENT_FACTORY(creds)
                cl.max_wait_s = 0  # поиск боя не ждёт сброса лимита: сразу объясняем, что случилось
                return self._json(inspect_report(cl, body["url"], body.get("fight")))
            if path == "/api/analyze":
                params = {k: body.get(k) for k in ("mode", "demo", "url", "fight", "actor", "ref", "against",
                                                   "refresh", "max_age_days", "pick", "mythic")}
                if params["mode"] not in (None, "raid", "raidrot"):
                    params["mode"] = None
                return self._json({"job": start_job(creds, params)})
            if path == "/api/refs/refresh":
                params = {"mode": "refresh", "refs": body.get("refs") or [], "max_age_days": body.get("max_age_days")}
                return self._json({"job": start_job(creds, params)})
            if path == "/api/job/done":
                # Браузер забрал результат и Excel — сервер удаляет их сразу, не дожидаясь часа
                with JOBS_LOCK:
                    job = JOBS.get(str(body.get("job", "")))
                    if job and job["state"] != "running":
                        JOBS.pop(job["id"], None)
                return self._json({"ok": True})
            if path in ("/api/save", "/api/open"):
                # Только Android-приложение: встроенное окно не умеет скачивать файлы и открывать ссылки
                from .platform_support import android_open_url, android_save_download, app_mode
                if SERVER["public"] or not self._is_local() or app_mode() != "android":
                    return self._json({"error": "Доступно только в Android-приложении"}, 403)
                if path == "/api/open":
                    url = str(body.get("url", ""))
                    if not url.startswith(("https://", "http://")):
                        raise ValueError("Неверная ссылка")
                    android_open_url(url)
                    return self._json({"ok": True})
                import base64
                name = re.sub(r'[\\/:*?"<>|]+', "_", str(body.get("name") or "file"))[:120]
                where = android_save_download(name, base64.b64decode(body.get("data") or ""),
                                              str(body.get("mime") or "application/octet-stream"))
                return self._json({"saved": where})
            if path == "/api/phone":
                if SERVER["public"] or not self._is_local():
                    return self._json({"error": "Доступно только на компьютере с программой"}, 403)
                ch = {}
                if "enabled" in body:
                    ch["lan"] = bool(body["enabled"])
                if body.get("new_token"):
                    ch["token"] = secrets.token_urlsafe(12)
                settings.save(**ch)
                return self._json({"ok": True})
            return self._json({"error": "Не найдено"}, 404)
        except SystemExit:
            self._json({"error": NEED_KEY, "need_key": True}, 400)
        except (ValueError, LookupError, KeyError, TypeError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            self._json({"error": _friendly(e)}, 500)


def _quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s)


def _free_port(preferred: int, host: str) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 50)):
        with socket.socket() as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    return 0


def serve(port: int = 8765, open_browser: bool = True, local_only: bool = False, public: bool = False) -> None:
    public = public or os.environ.get("WCL_PUBLIC") == "1"
    if public:  # публичный сервер ничего не пишет на диск — и игровые данные держит только в памяти
        from . import game_data
        game_data.SAVE["enabled"] = False
    if public:
        host, port = "0.0.0.0", int(os.environ.get("PORT", port))
        open_browser = False
    else:
        host = "127.0.0.1" if local_only else "0.0.0.0"
        port = _free_port(port, host)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    SERVER.update(port=httpd.server_address[1], lan=not local_only and not public, public=public)
    _CACHE["obj"] = None
    if public:
        print(f"WCL Analyzer работает как сервис на порту {SERVER['port']}. "
              "Данные пользователей хранятся только в их браузерах.")
    else:
        url = f"http://127.0.0.1:{SERVER['port']}/"
        print(f"WCL Analyzer открыт в браузере: {url}")
        if SERVER["lan"] and settings.load()["lan"] and lan_ip():
            print("С телефона в той же Wi-Fi сети: нажмите «На другом устройстве» и отсканируйте QR-код.")
        print("Не закрывайте это окно, пока пользуетесь программой. Остановить — Ctrl+C.")
        if open_browser:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")

"""Настройки и данные пользователей.

- settings.json (в каталоге данных) — настройки локального режима: доступ с телефона и его ключ.
- SQLite (тот же файл, что и кэш API) — эталоны и настройки каждого пользователя.
  Пользователь — это браузер (cookie), в локальном режиме и в командной строке — «local».
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from .config import data_dir

LOCAL_UID = "local"
DEFAULTS = {"lan": True, "token": ""}
USER_DEFAULTS = {"ref_max_age_days": 3, "auto_refresh": True}
_lock = threading.Lock()


def _settings_file() -> Path:
    return data_dir() / "settings.json"


def load() -> dict:
    with _lock:
        f = _settings_file()
        data = {}
        if f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        out = {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
        if not out["token"]:
            out["token"] = secrets.token_urlsafe(12)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        return out


def save(**changes) -> dict:
    cur = load()
    with _lock:
        cur.update({k: v for k, v in changes.items() if k in DEFAULTS})
        _settings_file().write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    return cur


# --------------------------------------------------------- данные пользователей
def _db(path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False, timeout=30)
    db.execute("CREATE TABLE IF NOT EXISTS user_refs ("
               " uid TEXT, key TEXT, encounter_id INTEGER, boss TEXT, cls TEXT, spec TEXT,"
               " difficulty INTEGER, top_n INTEGER, duration REAL, label TEXT, n_logs INTEGER,"
               " collected_at REAL, used_at REAL, PRIMARY KEY (uid, key))")
    db.execute("CREATE TABLE IF NOT EXISTS user_prefs (uid TEXT PRIMARY KEY, prefs TEXT)")
    db.commit()
    return db


def user_prefs(db_path, uid: str) -> dict:
    with _lock:
        db = _db(db_path)
        row = db.execute("SELECT prefs FROM user_prefs WHERE uid = ?", (uid,)).fetchone()
        db.close()
    data = json.loads(row[0]) if row else {}
    return {**USER_DEFAULTS, **{k: v for k, v in data.items() if k in USER_DEFAULTS}}


def save_user_prefs(db_path, uid: str, **changes) -> dict:
    cur = user_prefs(db_path, uid)
    cur.update({k: v for k, v in changes.items() if k in USER_DEFAULTS})
    with _lock:
        db = _db(db_path)
        db.execute("INSERT OR REPLACE INTO user_prefs VALUES (?, ?)", (uid, json.dumps(cur)))
        db.commit()
        db.close()
    return cur


def max_age_s(db_path=None, uid: str = LOCAL_UID) -> float:
    if db_path is None:
        from .config import cache_path
        db_path = cache_path()
    return float(user_prefs(db_path, uid)["ref_max_age_days"]) * 86400


def ref_key(encounter_id, cls, spec, difficulty, top_n, site: str | None = None) -> str:
    """Ключ эталона. У Classic, SoD и т. п. номера боссов пересекаются (Molten Core есть на нескольких сайтах),
    поэтому версия игры — в ключе; у основной игры ключ прежний."""
    key = f"{encounter_id}:{cls}:{spec}:{difficulty}:{top_n}"
    return key if site in (None, "", "www") else f"{key}:{site}"


def save_ref(db_path, uid: str = LOCAL_UID, **row) -> None:
    key = ref_key(row["encounter_id"], row["cls"], row["spec"], row["difficulty"], row["top_n"], row.get("site"))
    with _lock:
        db = _db(db_path)
        db.execute("INSERT OR REPLACE INTO user_refs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (uid, key, row["encounter_id"], row.get("boss", ""), row["cls"], row["spec"],
                    row["difficulty"], row["top_n"], row.get("duration"), row.get("label", ""),
                    row.get("n_logs", 0), row["collected_at"], time.time()))
        db.commit()
        db.close()


def list_refs(db_path, uid: str = LOCAL_UID) -> list[dict]:
    if not Path(db_path).exists():
        return []
    with _lock:
        db = _db(db_path)
        cols = ["uid", "key", "encounter_id", "boss", "cls", "spec", "difficulty", "top_n", "duration", "label",
                "n_logs", "collected_at", "used_at"]
        rows = [dict(zip(cols, r)) for r in
                db.execute("SELECT * FROM user_refs WHERE uid = ? ORDER BY used_at DESC", (uid,))]
        db.close()
    limit = max_age_s(db_path, uid)
    now = time.time()
    for r in rows:
        r.pop("uid")
        parts = str(r["key"]).split(":")
        r["site"] = parts[5] if len(parts) > 5 else "www"
        r["age_days"] = (now - r["collected_at"]) / 86400
        r["stale"] = now - r["collected_at"] > limit
    return rows


def move_user(db_path, old_uid: str, new_uid: str) -> None:
    """Перенос эталонов при «переносе на другое устройство» — объединяем с имеющимися."""
    if old_uid == new_uid:
        return
    with _lock:
        db = _db(db_path)
        db.execute("UPDATE OR IGNORE user_refs SET uid = ? WHERE uid = ?", (new_uid, old_uid))
        db.execute("DELETE FROM user_refs WHERE uid = ?", (old_uid,))
        db.commit()
        db.close()


def import_refs(db_path, uid: str, refs: list[dict]) -> int:
    """Восстановление эталонов из копии в браузере: добавляет отсутствующие и более свежие."""
    have = {r["key"]: r for r in list_refs(db_path, uid)}
    n = 0
    for r in refs[:200]:
        try:
            row = {"encounter_id": int(r["encounter_id"]), "boss": str(r.get("boss", ""))[:80],
                   "cls": str(r["cls"])[:40], "spec": str(r["spec"])[:40], "difficulty": int(r["difficulty"]),
                   "top_n": int(r["top_n"]), "duration": float(r["duration"]) if r.get("duration") else None,
                   "label": str(r.get("label", ""))[:80], "n_logs": int(r.get("n_logs", 0)),
                   "collected_at": float(r["collected_at"])}
            from .config import site_key
            row["site"] = site_key(r.get("site"))
        except (KeyError, TypeError, ValueError):
            continue
        key = ref_key(row["encounter_id"], row["cls"], row["spec"], row["difficulty"], row["top_n"], row["site"])
        if key not in have or have[key]["collected_at"] < row["collected_at"]:
            save_ref(db_path, uid=uid, **row)
            n += 1
    return n

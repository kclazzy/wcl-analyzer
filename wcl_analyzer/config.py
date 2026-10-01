"""Настройки: ключи API, адреса, путь к кэшу."""
from __future__ import annotations

import os
from pathlib import Path

API_URL = "https://www.warcraftlogs.com/api/v2/client"
TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
SITE_URL = "https://www.warcraftlogs.com"

# Сложности WCL: 3 = Normal, 4 = Heroic, 5 = Mythic
DIFFICULTY_NAMES = {1: "поиск рейда", 3: "обычный", 4: "героический", 5: "эпохальный"}


def load_env(path: str | Path = ".env") -> None:
    """Подхватывает KEY=VALUE из файла .env, не перезаписывая уже заданные переменные."""
    p = Path(path)
    if not p.exists() and os.environ.get("WCL_DATA_DIR"):
        p = Path(os.environ["WCL_DATA_DIR"]) / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def credentials() -> tuple[str, str]:
    load_env()
    cid = os.environ.get("WCL_CLIENT_ID", "")
    secret = os.environ.get("WCL_CLIENT_SECRET", "")
    if not cid or not secret:
        raise SystemExit(
            "Не заданы WCL_CLIENT_ID и WCL_CLIENT_SECRET.\n"
            "Создайте клиента на https://www.warcraftlogs.com/api/clients "
            "и впишите значения в файл .env (см. .env.example)."
        )
    return cid, secret


def data_dir() -> Path:
    """Каталог данных: кэш API, эталоны, отчёты, настройки. На сервере — постоянный диск."""
    load_env()
    if os.environ.get("WCL_DATA_DIR"):
        return Path(os.environ["WCL_DATA_DIR"])
    from .platform_support import default_data_dir
    d = default_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_path() -> Path:
    load_env()
    return Path(os.environ.get("WCL_CACHE_DB", data_dir() / "wcl_cache.sqlite"))


def reports_dir() -> Path:
    return data_dir() / "reports"

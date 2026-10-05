"""План сейвов по составу — без своего лога.

Состав — экспорт аддона WoWUtils Group Export (в игре /wugx → Ctrl+C) или простой список «Имя Класс Спек».
Бой — лучшие киллы этого босса на этой сложности из рейтинга Warcraft Logs: образцом берётся килл, чья
длительность ближе всего к указанным минутам. Пики урона и самые тяжёлые моменты образца — это моменты, на
которые нужны сейвы; что на них жмут лучшие гильдии — подсказка, какие кулдауны ставить. Кулдауны —
из состава (классы и спеки; кулдауны-таланты — если их жмут лучшие гильдии), с перезарядкой.
Время в заметке MRT — от пулла, со 2-й фазы — от начала фазы, как в обычном плане.
"""
from __future__ import annotations

import json
import re

from .compare import _fmt_t
from .config import DIFFICULTY_NAMES, SITE_URL

PAGES = 3          # страниц рейтинга WCL (по 100 киллов), среди которых ищем киллы нужной длительности
KILLS = 3          # сколько киллов топа загрузить: первый — образец по времени, все — что жмут лучшие
NEAR_S = 10        # самый тяжёлый момент дальше 10 с от пика механики — отдельный пик плана

ROLE_RU = {"tank": "Танк", "healer": "Лекарь", "melee": "DPS", "ranged": "DPS", "dps": "DPS", "damage": "DPS"}
# Список «Имя Класс Спек»: классы и спеки по-английски или по-русски
CLASS_ALIASES = {
    "deathknight": "DeathKnight", "dk": "DeathKnight", "рыцарьсмерти": "DeathKnight", "дк": "DeathKnight",
    "demonhunter": "DemonHunter", "dh": "DemonHunter", "охотникнадемонов": "DemonHunter",
    "druid": "Druid", "друид": "Druid", "evoker": "Evoker", "пробудитель": "Evoker",
    "hunter": "Hunter", "охотник": "Hunter", "mage": "Mage", "маг": "Mage", "monk": "Monk", "монах": "Monk",
    "paladin": "Paladin", "паладин": "Paladin", "priest": "Priest", "жрец": "Priest", "rogue": "Rogue",
    "разбойник": "Rogue", "shaman": "Shaman", "шаман": "Shaman", "warlock": "Warlock", "чернокнижник": "Warlock",
    "warrior": "Warrior", "воин": "Warrior",
}
HEAL_SPECS = {("Druid", "Restoration"), ("Shaman", "Restoration"), ("Priest", "Holy"), ("Priest", "Discipline"),
              ("Paladin", "Holy"), ("Monk", "Mistweaver"), ("Evoker", "Preservation")}
TANK_SPECS = {("DeathKnight", "Blood"), ("DemonHunter", "Vengeance"), ("Druid", "Guardian"), ("Monk", "Brewmaster"),
              ("Paladin", "Protection"), ("Warrior", "Protection")}


def _cls(name: str) -> str:
    k = re.sub(r"[^\w]+", "", str(name or "")).lower()
    return CLASS_ALIASES.get(k) or str(name or "").replace(" ", "")


SPEC_ALIASES = {"восстановление": "Restoration", "исцеление": "Restoration", "дисциплина": "Discipline",
                "холи": "Holy", "рестор": "Restoration", "рдруид": "Restoration", "превока": "Preservation",
                "мв": "Mistweaver", "бм": "BeastMastery", "лед": "Frost", "лёд": "Frost"}


def _spec(name: str, cls: str = "") -> str:
    """Спек по-английски (как в таблице кулдаунов): «Beast Mastery», «Исцеление», «Послушание» → BeastMastery,
    Restoration, Discipline. Русские названия — из names_ru (как в игре) с учётом класса."""
    from .names_ru import SPECS
    raw = re.sub(r"\s+", " ", str(name or "")).strip()
    k = raw.lower().replace("ё", "е")
    for (c, sp), ru in SPECS.items():
        if ru.lower().replace("ё", "е") == k and (not cls or c == cls):
            return sp
    return SPEC_ALIASES.get(k) or raw.replace(" ", "")


def _role(cls: str, spec: str, given: str | None = None) -> str:
    if (cls, spec) in HEAL_SPECS:
        return "Лекарь"
    if (cls, spec) in TANK_SPECS:
        return "Танк"
    return ROLE_RU.get(str(given or "").lower(), "DPS")


def parse_roster(text: str) -> tuple[list[dict], dict]:
    """Состав из экспорта WoWUtils Group Export (JSON) или из списка строк «Имя Класс Спек».
    → ([{name, cls, spec, role}], {source, guild}). Пустой или непонятный текст — LookupError."""
    text = (text or "").strip()
    if not text:
        raise LookupError("Вставьте состав: экспорт аддона WoWUtils Group Export (в игре /wugx) или строки «Имя Класс Спек»")
    players, meta = [], {"source": "list", "guild": ""}
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as e:
            raise LookupError(f"Экспорт WoWUtils не читается — скопируйте окно аддона целиком (Ctrl+A, Ctrl+C): {e}") from None
        meta = {"source": "wowutils", "guild": str((data.get("metadata") or {}).get("exportedFrom") or "")}
        for m in data.get("members") or []:
            chars = m.get("characters") or [{}]
            main = next((c for c in chars if c.get("name") == m.get("displayName")), chars[0])
            cls = _cls(main.get("playerClass"))
            spec = _spec(main.get("playerSpec"), cls)
            if main.get("name") and cls:
                players.append({"name": str(main["name"]), "realm": str(main.get("realm") or ""), "cls": cls,
                                "spec": spec, "role": _role(cls, spec, m.get("mainRole"))})
    else:
        for line in text.splitlines():
            parts = [p for p in re.split(r"[\s,;\t]+", line.strip()) if p]
            if len(parts) < 2:
                continue
            name, rest = parts[0], parts[1:]
            # класс — где угодно после имени («Имя Класс Спек» или «Имя Спек Класс»), из одного или двух слов:
            # «Death Knight», «Demon Hunter», «Рыцарь смерти»
            found = None
            for i in range(len(rest)):
                for n in (3, 2, 1):   # «Охотник на демонов» — три слова
                    if i + n <= len(rest) and _cls(" ".join(rest[i:i + n])) in CLASS_ALIASES.values():
                        found = (i, n)
                        break
                if found:
                    break
            if not found:
                continue
            i, n = found
            cls = _cls(" ".join(rest[i:i + n]))
            spec = _spec(" ".join(rest[:i] + rest[i + n:]), cls)
            players.append({"name": name, "realm": "", "cls": cls, "spec": spec, "role": _role(cls, spec)})
    if not players:
        raise LookupError("В тексте не нашлось ни одного игрока — нужен экспорт WoWUtils Group Export или строки «Имя Класс Спек»")
    seen, out = set(), []
    for p in players:   # один игрок — имя и сервер: тёзки с разных серверов — разные игроки
        key = (p["name"].lower(), p.get("realm", "").lower())
        if key not in seen:
            seen.add(key)
            out.append(p)
    names = [p["name"] for p in out]
    for p in out:       # тёзкам — имя с сервером, как в игре: «Имя-Сервер»
        if names.count(p["name"]) > 1 and p.get("realm"):
            p["name"] = f"{p['name']}-{p['realm'].replace(' ', '')}"
    return out, meta


CHOICE = [(322118, 325197)]   # узлы выбора без пометки в таблице: Юй-лун или Цзи-Жэнь


def roster_cooldowns(players: list[dict], duration: float, top_use: dict | None = None, n_kills: int = 0) -> list[dict]:
    """Рейдовые кулдауны состава по классу и спеку (как roster_cds у разбора лога). Талантов в экспорте нет:
    основные кулдауны спека — всегда, а кулдауны-таланты (Апофеоз, Перерождение, Древо Жизни…) — если их
    жмут хотя бы в половине загруженных киллов топа. Из двух вариантов одного узла — тот, что у топа чаще."""
    from .raid_cds import candidates
    from . import game_data
    top_use = top_use or {}
    pairs = list(CHOICE) + [(int(x["id"]), int(x["choice_with"])) for x in game_data.raid_cds() if x.get("choice_with")]
    out = []
    for i, p in enumerate(players):
        got = {}
        for sid, name, cd, core in candidates(p["cls"], p["spec"]):
            if core:
                got[sid] = (name, cd, "обычно есть у спека")
            elif n_kills and top_use.get(sid, 0) >= max(1, n_kills / 2):
                got[sid] = (name, cd, f"талант: у топа жмут в {top_use[sid]} из {n_kills} киллов")
        for a, b in pairs:  # из узла выбора — один вариант
            if a in got and b in got:   # остаётся тот, что у топа чаще; поровну — основной кулдаун спека
                rank = {x: (top_use.get(x, 0), "талант" not in got[x][2], -x) for x in (a, b)}
                got.pop(min((a, b), key=rank.get))
        common = {int(x["id"]): float(x["cd_common"]) for x in game_data.raid_cds() if x.get("cd_common")}
        for sid, (name, cd, source) in got.items():
            cd = common.get(sid, cd)   # откат с талантом, который берут почти все (Мрак, Перемотка)
            out.append({"pid": i + 1, "player": p["name"], "role": p["role"], "cls": p["cls"], "spec": p["spec"],
                        "id": sid, "name": name, "cd": cd, "used": 0,
                        "max_uses": int(duration // cd) + 1 if duration else None, "source": source})
    out.sort(key=lambda c: (c["role"] != "Лекарь", c["player"], -c["cd"]))
    return out


def _dur_s(rk: dict) -> float | None:
    d = rk.get("duration")
    try:
        d = float(d)
    except (TypeError, ValueError):
        return None
    return d / 1000.0 if d > 10000 else d


def pick_kills(client, encounter_id: int, difficulty: int, target_s: float, n: int = KILLS,
               size: int | None = None) -> list[dict]:
    """Киллы из рейтинга WCL (по скорости, до PAGES страниц), отсортированные по близости длительности к target_s."""
    pool = []
    for page in range(1, PAGES + 1):
        try:
            from .api import size_kw
            got = client.fight_rankings(encounter_id, difficulty, "speed", page=page, **size_kw(size))
        except Exception:  # noqa: BLE001 — следующая страница не обязательна
            if not pool:
                raise
            break
        pool += [r for r in got if (r.get("report") or {}).get("code")]
        if len(got) < 100 or (pool and max((_dur_s(r) or 0) for r in pool) >= target_s + 30):
            break   # рейтинг по скорости: дальше киллы только длиннее
    pool = [r for r in pool if _dur_s(r)]
    pool.sort(key=lambda r: abs(_dur_s(r) - target_s))
    return pool[: n + 3]


def _phase_of(t: float, phases: list[dict]) -> int | None:
    cur = None
    for p in phases or []:
        if p["t"] <= t + 1e-6:
            cur = p["n"]
    return cur


def plan_events(R: dict) -> list[dict]:
    """Моменты для сейвов из боя-образца: пики урона механик и самые тяжёлые 5 секунд боя (если тяжёлый
    момент не совпадает с пиком механики, он становится отдельным пиком плана)."""
    X, phases = R["extras"], R["info"].get("phases") or []
    spikes = [dict(s) for s in X.get("spikes") or []]
    count: dict = {}
    for s in spikes:
        count[s.get("ability_id")] = max(count.get(s.get("ability_id"), 0), int(s.get("k", 1)))
    for h in sorted(X.get("heaviest") or [], key=lambda h: h["t"]):
        if any(abs(h["t"] - s["t"]) <= NEAR_S or abs(h["t"] - s.get("peak_t", s["t"])) <= NEAR_S for s in spikes):
            continue
        # свой ключ (отрицательный id): не сдвигает нумерацию настоящих пиков этой способности и не
        # сопоставляется с «№ k» у топа
        extra = -(len(count) + 1000)
        count[extra] = 1
        name = (h.get("abilities") or ["урон по рейду"])[0]
        spikes.append({"t": h["t"], "peak_t": h["t"] + 2, "time": h["time"], "damage": h["damage"],
                       "ability": f"Тяжёлый момент: {name}", "ability_id": extra, "k": 1,
                       "phase": _phase_of(h["t"], phases), "heavy_moment": True})
    return sorted(spikes, key=lambda s: s["t"])


def run_roster_plan(client, encounter_id: int, difficulty: int, minutes: float, roster_text: str,
                    log=print, progress=lambda x: None, size: int | None = None) -> dict:
    from .api import WCLError
    from .raid_top import _aggregate, light_raid, make_plan, mrt_note

    players, meta = parse_roster(roster_text)
    target = max(60.0, float(minutes) * 60.0)
    heal = sum(1 for p in players if p["role"] == "Лекарь")
    log(f"Состав: {len(players)} игроков, лекарей: {heal}.")
    if not roster_cooldowns(players, target):
        raise LookupError("В составе нет ни одного рейдового кулдауна — проверьте классы и спеки в экспорте")
    log(f"Ищу в рейтинге Warcraft Logs киллы длительностью около {_fmt_t(target)}…")
    cands = pick_kills(client, encounter_id, difficulty, target, size=size)
    if not cands:
        raise LookupError("У этого босса на этой сложности пока нет киллов в рейтинге Warcraft Logs")
    kills, base = [], None

    def load(rk):
        """Один килл: (R, k) или (None, причина). Лимит — исключением наверх."""
        rep = rk["report"]
        code, fid = rep["code"], int(rep.get("fightID") or rep.get("fightId") or 0)
        guild = (rk.get("guild") or {}).get("name") or rk.get("name") or code
        try:
            report = client.report(code)
            meta_ph = report.get("phases") if "phases" in report else None   # названия фаз — уже в отчёте
            if meta_ph is None and hasattr(client, "report_phases"):
                try:
                    meta_ph = client.report_phases(code)
                except Exception:  # noqa: BLE001 — названия фаз необязательны
                    meta_ph = None
            R = light_raid(client, code, fid, report=report, difficulty=difficulty, encounter_id=encounter_id)
            if meta_ph:  # названия фаз, как в обычном разборе
                from .raid import fight_phases
                f = next(x for x in report["fights"] if int(x["id"]) == fid)
                R["info"]["phases"] = fight_phases(f, meta_ph, R["info"]["duration_s"])
        except WCLError as e:
            if "лимит" in str(e).lower():
                raise
            return None, f"{guild}: {e}"
        except (LookupError, StopIteration, KeyError) as e:
            return None, f"{guild}: {e}"
        return (R, {"guild": guild, "duration": R["info"]["duration_s"], "code": code, "fight": fid,
                    "spikes": R["extras"]["spikes"], "cds": R["extras"]["raid_cds"]}), None

    # Киллы — параллельно (раньше по одному); запасной — только если какой-то не открылся. Порядок — как в отборе
    from concurrent.futures import ThreadPoolExecutor
    from .collect import parallel_workers
    with ThreadPoolExecutor(max_workers=parallel_workers(client)) as pool:
        futs = [pool.submit(load, rk) for rk in cands[:KILLS]]
        nxt, i = len(futs), 0
        while i < len(futs) and len(kills) < KILLS:
            got, why = futs[i].result()
            i += 1
            if got is None:
                log(f"  пропущен килл {why}")
                if nxt < len(cands):
                    futs.append(pool.submit(load, cands[nxt]))
                    nxt += 1
                continue
            R_, k = got
            kills.append(k)
            if base is None:
                base = (R_, k)
            log(f"  Килл {len(kills)}: {k['guild']}, {_fmt_t(k['duration'])}")
            progress(0.15 + 0.7 * len(kills) / KILLS)
        for f_ in futs[i:]:
            f_.cancel()
    if not base:
        raise LookupError("Не удалось загрузить ни одного килла из рейтинга (логи закрыты или недоступны)")
    R, bk = base
    from collections import Counter
    top_use = Counter(sid for k in kills for sid in {int(c["id"]) for c in k["cds"]})
    cds = roster_cooldowns(players, target, top_use, len(kills))
    log(f"Рейдовых кулдаунов в составе: {len(cds)} (таланты — по тому, что жмут лучшие гильдии).")
    phases = R["info"].get("phases") or []
    names = {}
    for k in kills:
        for c in k["cds"]:
            names.setdefault(c["id"], c["name"])
    for c in cds:
        names[c["id"]] = c["name"]
    events = plan_events(R)
    base = getattr(client, "site_url", None) or SITE_URL   # сайт той версии игры (Classic, SoD…)
    X = {"roster_cds": cds, "spikes": events}
    plan = make_plan(X, _aggregate(kills), [], names, phases, _aggregate(kills, by_phase=True))
    boss = R["info"]["boss"]
    note = mrt_note(plan, boss)
    progress(0.97)
    log(f"Образец — килл {bk['guild']} ({_fmt_t(bk['duration'])}): пиков в плане — {len(plan)}.")
    return {
        "mode": "rosterplan",
        "info": {"boss": boss, "difficulty": DIFFICULTY_NAMES.get(int(difficulty), ""), "minutes": minutes,
                 "target": _fmt_t(target), "phases": phases, "guild": meta.get("guild") or "",
                 "roster_source": meta["source"], "players": len(players), "healers": heal,
                 "base": {"guild": bk["guild"], "duration": _fmt_t(bk["duration"]),
                          "url": f"{base}/reports/{bk['code']}#fight={bk['fight']}"}},
        "kills": [{"guild": k["guild"], "duration": _fmt_t(k["duration"]),
                   "url": f"{base}/reports/{k['code']}#fight={k['fight']}"} for k in kills],
        "players": players, "roster_cds": cds, "plan": plan, "mrt": note,
        "heaviest": sorted(R["extras"].get("heaviest") or [], key=lambda h: h["t"]),
        "unknown": [p["name"] for p in players if not any(c["player"] == p["name"] for c in cds)
                    and p["role"] == "Лекарь"],
    }

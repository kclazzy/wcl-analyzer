"""Модель лога игрока и её сборка из событий WCL.

Все времена — секунды от пулла (rel_s). Одна и та же функция build_player_log
используется и для реальных ответов API, и для демо-данных.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import mean

from .config import DIFFICULTY_NAMES, SITE_URL

# Типы ресурсов WCL -> название
RESOURCE_NAMES = {
    0: "Мана", 1: "Ярость", 2: "Концентрация", 3: "Энергия", 4: "Комбо-очки",
    6: "Сила рун", 7: "Осколки души", 8: "Астральная сила", 9: "Энергия Света",
    11: "Водоворот", 12: "Ци", 13: "Безумие", 16: "Чародейские заряды",
    17: "Гнев", 18: "Боль", 19: "Сущность", 99: "Осколки (демо)",
}


@dataclass
class Cast:
    t: float                 # момент каста (конец произнесения)
    id: int
    start: float             # начало произнесения (для мгновенных = t)
    res: tuple | None = None  # (amount, max, type) ресурса в момент каста
    hp: float | None = None   # HP игрока, %


@dataclass
class PlayerLog:
    name: str
    cls: str
    spec: str
    server: str
    report_code: str
    fight_id: int
    actor_id: int
    encounter_id: int
    encounter_name: str
    difficulty: int
    kill: bool
    duration: float
    report_start: float = 0.0
    dps: float = 0.0
    ilvl: float | None = None
    rank: int | None = None
    casts: list[Cast] = field(default_factory=list)
    buffs: dict[int, list[tuple[float, float]]] = field(default_factory=dict)
    buff_events: list[tuple[float, str, int, int]] = field(default_factory=list)
    debuffs: dict[int, list[tuple[float, float]]] = field(default_factory=dict)
    dmg_taken: list[tuple[float, int, float, float | None]] = field(default_factory=list)
    deaths: list[float] = field(default_factory=list)
    boss_casts: list[tuple[float, int]] = field(default_factory=list)
    dmg_by_ability: dict[int, float] = field(default_factory=dict)
    dmg_timeline: list[tuple[float, float]] = field(default_factory=list)
    names: dict[int, str] = field(default_factory=dict)
    resource_name: str = ""
    trinket_ids: list[int] = field(default_factory=list)
    # --- расширенные данные
    dps_calc: float = 0.0                       # DPS по таблице урона (для самопроверки)
    boss_ids: set = field(default_factory=set)  # id боссов этого боя
    boss_debuffs: dict[int, list[tuple[float, float]]] = field(default_factory=dict)  # все дебаффы на боссе
    boss_debuff_src: dict[int, set] = field(default_factory=dict)  # кто их накладывал
    cast_targets: dict[int, int] = field(default_factory=dict)     # цель -> число кастов
    talents: list[int] = field(default_factory=list)
    res_gain: float | None = None               # получено основного ресурса (события Resources)
    res_waste: float | None = None              # потеряно сверх максимума
    phases: list[tuple[float, int]] = field(default_factory=list)  # (начало, номер фазы)

    @property
    def difficulty_name(self) -> str:
        return DIFFICULTY_NAMES.get(self.difficulty, str(self.difficulty))

    @property
    def url(self) -> str:
        return (f"{SITE_URL}/reports/{self.report_code}"
                f"#fight={self.fight_id}&type=casts&source={self.actor_id}")

    def link_at(self, t: float) -> str:
        return self.url

    def name_of(self, ability_id: int) -> str:
        return self.names.get(ability_id, f"Spell {ability_id}")


# ------------------------------------------------------------------ helpers
def parse_report_url(url: str) -> tuple[str, str | None, int | None]:
    """Код отчёта, бой ('last' или номер) и source из ссылки WCL."""
    m = re.search(r"reports/(?:a:)?([A-Za-z0-9]{8,})", url)
    if not m:
        if re.fullmatch(r"[A-Za-z0-9]{8,}", url.strip()):
            return url.strip(), None, None
        raise ValueError(f"Не похоже на ссылку Warcraft Logs: {url}")
    code = m.group(1)
    fm = re.search(r"fight=(\d+|last)", url)
    sm = re.search(r"source=(\d+)", url)
    return code, (fm.group(1) if fm else None), (int(sm.group(1)) if sm else None)


def _intervals(events: list[tuple[float, str, object]], duration: float,
               start_types: set[str], end_types: set[str]) -> dict:
    opened: dict = {}
    out: dict = defaultdict(list)
    for t, typ, key in sorted(events, key=lambda e: e[0]):
        if typ in start_types:
            opened.setdefault(key, t)
        elif typ in end_types:
            s = opened.pop(key, 0.0)  # снят без наложения = был до пулла
            out[key].append((s, t))
    for key, s in opened.items():
        out[key].append((s, duration))
    return out


def merge_intervals(iv: list[tuple[float, float]]) -> list[tuple[float, float]]:
    res: list[list[float]] = []
    for s, e in sorted(iv):
        if res and s <= res[-1][1]:
            res[-1][1] = max(res[-1][1], e)
        else:
            res.append([s, e])
    return [(a, b) for a, b in res]


def _hp_pct(ev: dict) -> float | None:
    hp, mx = ev.get("hitPoints"), ev.get("maxHitPoints")
    if hp is None:
        return None
    if mx and mx > 100 and hp <= mx:
        return 100.0 * hp / mx
    return float(hp) if hp <= 100 else None


def _pick_resource(ev: dict) -> tuple | None:
    res = ev.get("classResources") or []
    if not res:
        return None
    non_mana = [r for r in res if r.get("type") not in (0, None)]
    r = (non_mana or res)[0]
    if r.get("max") in (None, 0):
        return None
    return (float(r.get("amount", 0)), float(r["max"]), int(r.get("type", -1)))


def _ilvl_from_gear(gear: list[dict]) -> float | None:
    levels = [g.get("itemLevel", 0) for i, g in enumerate(gear or [])
              if i not in (3, 18) and (g.get("itemLevel") or 0) > 1]
    return round(mean(levels), 1) if levels else None


def _damage_by_ability(table: dict) -> dict[int, float]:
    entries = (table or {}).get("entries") or []
    out: dict[int, float] = defaultdict(float)
    for e in entries:
        if "abilities" in e:  # таблица по игрокам: берём разбивку по способностям
            for a in e["abilities"]:
                out[int(a.get("guid", 0))] += float(a.get("total", 0))
        else:
            out[int(e.get("guid", 0))] += float(e.get("total", 0))
    return dict(out)


# -------------------------------------------------------------------- build
def build_player_log(report: dict, fight: dict, actor: dict, raw: dict,
                     spec: str, cls: str, dps: float | None = None,
                     rank: int | None = None) -> PlayerLog:
    f0 = float(fight["startTime"])
    duration = (float(fight["endTime"]) - f0) / 1000.0
    aid = int(actor["id"])
    rel = lambda ev: (float(ev["timestamp"]) - f0) / 1000.0  # noqa: E731

    names = {int(a["gameID"]): a["name"] for a in report["masterData"]["abilities"]}
    boss_ids = {int(a["id"]) for a in report["masterData"]["actors"]
                if a.get("subType") == "Boss"}

    log = PlayerLog(
        name=actor["name"], cls=cls, spec=spec, server=actor.get("server") or "",
        report_code=report["code"], fight_id=int(fight["id"]), actor_id=aid,
        encounter_id=int(fight.get("encounterID") or 0),
        encounter_name=fight.get("name", ""), difficulty=int(fight.get("difficulty") or 0),
        kill=bool(fight.get("kill")), duration=duration,
        report_start=float(report.get("startTime") or 0), rank=rank, names=names,
    )

    # Касты: begincast даёт начало произнесения, cast — момент применения
    pending: dict[int, float] = {}
    for ev in sorted(raw.get("casts", []), key=lambda e: e["timestamp"]):
        if int(ev.get("sourceID", aid)) != aid:
            continue
        t, ab = rel(ev), int(ev.get("abilityGameID", 0))
        if ev.get("type") == "begincast":
            pending[ab] = t
        elif ev.get("type") == "cast":
            st = pending.pop(ab, t)
            if t - st > 6:
                st = t
            log.casts.append(Cast(t=t, id=ab, start=st, res=_pick_resource(ev), hp=_hp_pct(ev)))
    res_types = [c.res[2] for c in log.casts if c.res]
    if res_types:
        rt = max(set(res_types), key=res_types.count)
        log.resource_name = RESOURCE_NAMES.get(rt, f"Ресурс {rt}")

    # Баффы на игроке
    bev = []
    for ev in raw.get("buffs", []):
        if int(ev.get("targetID", aid)) != aid:
            continue
        t, typ, ab = rel(ev), ev.get("type", ""), int(ev.get("abilityGameID", 0))
        log.buff_events.append((t, typ, ab, int(ev.get("sourceID", -1))))
        bev.append((t, typ, ab))
    log.buff_events.sort()
    log.buffs = {k: merge_intervals(v) for k, v in _intervals(
        bev, duration, {"applybuff"}, {"removebuff"}).items()}

    # Дебаффы игрока на боссе (или на цели с наибольшим числом дебаффов)
    dev = [(rel(ev), ev.get("type", ""), (int(ev.get("abilityGameID", 0)), int(ev.get("targetID", -1))))
           for ev in raw.get("debuffs", []) if int(ev.get("sourceID", aid)) == aid]
    per_target = _intervals(dev, duration, {"applydebuff"}, {"removedebuff"})
    targets = defaultdict(float)
    for (ab, tgt), iv in per_target.items():
        targets[tgt] += sum(e - s for s, e in iv)
    main_targets = boss_ids & set(targets) or ({max(targets, key=targets.get)} if targets else set())
    deb: dict[int, list] = defaultdict(list)
    for (ab, tgt), iv in per_target.items():
        if tgt in main_targets:
            deb[ab].extend(iv)
    log.debuffs = {k: merge_intervals(v) for k, v in deb.items()}

    # Все дебаффы на боссе — от рейда, от самого игрока и от самого босса (окна уязвимости)
    fight_npcs = {int(x["id"]) for x in fight.get("enemyNPCs") or [] if "id" in x}
    log.boss_ids = (boss_ids & fight_npcs) if fight_npcs and boss_ids & fight_npcs else set(boss_ids)
    if not log.boss_ids and main_targets:
        log.boss_ids = set(main_targets)
    bevs = raw.get("boss_debuffs")
    if bevs is None:
        bevs = raw.get("debuffs", [])
    bdev, bsrc = [], defaultdict(set)
    for ev in bevs:
        tgt = int(ev.get("targetID", -1))
        if log.boss_ids and tgt not in log.boss_ids:
            continue
        ab = int(ev.get("abilityGameID", 0))
        bdev.append((rel(ev), ev.get("type", ""), (ab, tgt)))
        if ev.get("type") == "applydebuff":
            bsrc[ab].add(int(ev.get("sourceID", -1)))
    bd: dict[int, list] = defaultdict(list)
    for (ab, _tgt), iv in _intervals(bdev, duration, {"applydebuff"}, {"removedebuff"}).items():
        bd[ab].extend(iv)
    log.boss_debuffs = {k: merge_intervals(v) for k, v in bd.items()}
    log.boss_debuff_src = dict(bsrc)

    tg: dict[int, int] = defaultdict(int)
    for ev in raw.get("casts", []):
        if ev.get("type") == "cast" and int(ev.get("sourceID", aid)) == aid:
            t_id = int(ev.get("targetID", -1))
            if t_id >= 0 and t_id != aid:
                tg[t_id] += 1
    log.cast_targets = dict(tg)

    # Ресурс: получено и потеряно сверх максимума (resourcechange.waste)
    if raw.get("resources"):
        gain, waste = defaultdict(float), defaultdict(float)
        for ev in raw["resources"]:
            if ev.get("type") != "resourcechange" or int(ev.get("targetID", aid)) != aid:
                continue
            rt = int(ev.get("resourceChangeType", -1))
            gain[rt] += max(0.0, float(ev.get("resourceChange", 0) or 0))
            waste[rt] += max(0.0, float(ev.get("waste", 0) or 0))
        main = [r for r in gain if r not in (0, -1)] or list(gain)
        if res_types:
            main_rt = max(set(res_types), key=res_types.count)
            if main_rt in gain:
                main = [main_rt]
        if main:
            rt = max(main, key=lambda r: gain[r])
            if gain[rt] > 0:
                log.res_gain, log.res_waste = gain[rt], waste[rt]

    log.phases = sorted(((float(p["startTime"]) - f0) / 1000.0, int(p.get("id", 0)))
                        for p in fight.get("phaseTransitions") or [] if p.get("startTime") is not None)

    for ev in raw.get("dmg_taken", []):
        if int(ev.get("targetID", -1)) != aid or ev.get("type") != "damage":
            continue
        amount = float(ev.get("amount", 0)) + float(ev.get("absorbed", 0) or 0)
        log.dmg_taken.append((rel(ev), int(ev.get("abilityGameID", 0)), amount, _hp_pct(ev)))
    log.dmg_taken.sort()

    log.deaths = sorted(rel(ev) for ev in raw.get("deaths", [])
                        if ev.get("type") == "death" and int(ev.get("targetID", -1)) == aid)

    for ev in raw.get("boss_casts", []):
        src = int(ev.get("sourceID", -1))
        if ev.get("type") != "cast" or (boss_ids and src not in boss_ids):
            continue
        log.boss_casts.append((rel(ev), int(ev.get("abilityGameID", 0))))
    log.boss_casts.sort()

    log.dmg_timeline = sorted((rel(ev), float(ev.get("amount", 0)))
                              for ev in raw.get("dmg_done", [])
                              if ev.get("type") == "damage" and int(ev.get("sourceID", -1)) == aid)

    log.dmg_by_ability = _damage_by_ability(raw.get("dmg_table", {}))
    total = sum(log.dmg_by_ability.values())
    log.dps_calc = total / duration if total and duration else 0.0
    if dps is not None:
        log.dps = float(dps)
    elif total and duration:
        log.dps = total / duration
    elif log.dmg_timeline and duration:
        log.dps = sum(a for _, a in log.dmg_timeline) / duration

    for ev in raw.get("combatant", []):
        if int(ev.get("sourceID", -1)) == aid:
            gear = ev.get("gear") or []
            log.ilvl = _ilvl_from_gear(gear)
            tree = ev.get("talentTree") or ev.get("talents") or []
            log.talents = sorted({int(x.get("nodeID") or x.get("id") or 0) * 10 + int(x.get("rank", 1) or 1)
                                  for x in tree if isinstance(x, dict) and (x.get("nodeID") or x.get("id"))})
            if len(gear) > 13:
                log.trinket_ids = [int(gear[12].get("id", 0)), int(gear[13].get("id", 0))]
            break
    return log


# ----------------------------------------------------------- API loading
def fetch_raw(client, report: dict, fight: dict, actor_id: int,
              with_damage_events: bool = False) -> dict:
    """Скачивает всё, что нужно для одного игрока в одном бою."""
    code, fid = report["code"], int(fight["id"])
    s, e = float(fight["startTime"]), float(fight["endTime"])
    if hasattr(client, "events_multi") and not getattr(client, "_no_batch", False):
        from .api import WCLError
        try:
            return _fetch_raw_batched(client, report, fight, actor_id, with_damage_events)
        except WCLError:
            client._no_batch = True  # сервер не принял общий запрос — дальше качаем по одному
    raw: dict = {}
    raw["casts"] = client.events(code, fid, s, e, "Casts", source_id=actor_id,
                                 include_resources=True)
    raw["buffs"] = client.events(code, fid, s, e, "Buffs", target_id=actor_id)
    raw["debuffs"] = client.events(code, fid, s, e, "Debuffs", source_id=actor_id,
                                   hostility="Enemies")
    # Дебаффы на боссе от всех источников: рейдовые, свои и окна уязвимости
    raw["boss_debuffs"] = []
    for bid in boss_actor_ids(report, fight)[:3]:
        raw["boss_debuffs"] += client.events(code, fid, s, e, "Debuffs", target_id=bid,
                                             hostility="Enemies")
    try:
        raw["resources"] = client.events(code, fid, s, e, "Resources", target_id=actor_id)
    except Exception:  # noqa: BLE001 — потери ресурса не обязательны
        raw["resources"] = []
    raw["dmg_taken"] = client.events(code, fid, s, e, "DamageTaken", target_id=actor_id,
                                     include_resources=True)
    raw["deaths"] = client.events(code, fid, s, e, "Deaths")
    raw["boss_casts"] = client.events(code, fid, s, e, "Casts", hostility="Enemies")
    raw["combatant"] = client.events(code, fid, s, s + 1, "CombatantInfo")
    raw["dmg_table"] = client.damage_table(code, fid, actor_id)
    if with_damage_events:
        raw["dmg_done"] = client.events(code, fid, s, e, "DamageDone", source_id=actor_id)
    return raw


def _fetch_raw_batched(client, report: dict, fight: dict, actor_id: int, with_damage_events: bool) -> dict:
    """То же, что fetch_raw, но одним запросом к API вместо десяти."""
    code, fid = report["code"], int(fight["id"])
    s, e = float(fight["startTime"]), float(fight["endTime"])
    specs = {
        "casts": {"data_type": "Casts", "source_id": actor_id, "include_resources": True},
        "buffs": {"data_type": "Buffs", "target_id": actor_id},
        "debuffs": {"data_type": "Debuffs", "source_id": actor_id, "hostility": "Enemies"},
        "resources": {"data_type": "Resources", "target_id": actor_id},
        "dmg_taken": {"data_type": "DamageTaken", "target_id": actor_id, "include_resources": True},
        "deaths": {"data_type": "Deaths"},
        "boss_casts": {"data_type": "Casts", "hostility": "Enemies"},
        "combatant": {"data_type": "CombatantInfo", "end": s + 1},
    }
    bosses = boss_actor_ids(report, fight)[:3]
    for i, bid in enumerate(bosses):
        specs[f"boss_debuffs_{i}"] = {"data_type": "Debuffs", "target_id": bid, "hostility": "Enemies"}
    if with_damage_events:
        specs["dmg_done"] = {"data_type": "DamageDone", "source_id": actor_id}
    got = client.events_multi(code, fid, s, e, specs, {"dmg_table": {"data_type": "DamageDone", "source_id": actor_id}})
    raw = {k: got[k] for k in specs if not k.startswith("boss_debuffs_")}
    raw["boss_debuffs"] = [ev for i in range(len(bosses)) for ev in got[f"boss_debuffs_{i}"]]
    raw["dmg_table"] = got["dmg_table"]
    return raw


def boss_actor_ids(report: dict, fight: dict) -> list[int]:
    bosses = [int(a["id"]) for a in report["masterData"]["actors"] if a.get("subType") == "Boss"]
    npcs = {int(x["id"]) for x in fight.get("enemyNPCs") or [] if "id" in x}
    return [b for b in bosses if b in npcs] if npcs and any(b in npcs for b in bosses) else bosses


def find_player(client, report: dict, fight: dict, name: str | None = None,
                actor_id: int | None = None) -> tuple[dict, str, str]:
    """Возвращает (actor, class, spec) игрока в бою."""
    details = client.player_details(report["code"], int(fight["id"]))
    players = []
    for role in ("dps", "healers", "tanks"):
        for p in details.get(role, []) or []:
            specs = p.get("specs") or []
            spec = (specs[0].get("spec") if specs and isinstance(specs[0], dict)
                    else (specs[0] if specs else p.get("icon", "").split("-")[-1]))
            players.append((int(p["id"]), p["name"], p.get("type", ""), spec, role))
    actors = {int(a["id"]): a for a in report["masterData"]["actors"]}
    if actor_id is not None:
        match = [p for p in players if p[0] == actor_id]
    elif name:
        match = [p for p in players if p[1].lower() == name.lower()]
    else:
        match = []
    if not match:
        listing = ", ".join(f"{p[1]} ({p[2]} {p[3]})" for p in players if p[4] == "dps")
        raise LookupError(f"Игрок не найден. Укажите --player. DPS в бою: {listing}")
    aid, _, cls, spec, _ = match[0]
    return actors.get(aid, {"id": aid, "name": match[0][1]}), cls, spec

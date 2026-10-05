"""Анализ всего рейда в одном бою: игроки, механики, смерти, пуллы.

Данные берутся запросами по всему бою (без фильтра по игроку), поэтому разбор рейда
стоит около 10 запросов к API плюс по одному запросу смертей на каждый пулл.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from statistics import median

import numpy as np

from .collect import _pick_fight
from .compare import _fmt_t, _n
from .config import DIFFICULTY_NAMES, SITE_URL

from .logs import boss_actor_ids, merge_intervals, parse_report_url
from .metrics import HEALTHSTONE_RE, LUST_IDS, LUST_RE, POTION_RE, RACIAL_RE

FLASK_RE = re.compile(r"flask|phial|фиал|настой|флакон|alchemical chaos", re.I)
FOOD_RE = re.compile(r"well fed|сытост|сытн|hearty|пищ|food|feast|пир", re.I)
RUNE_RE = re.compile(r"augment|руна усиления|кристаллиз", re.I)
CD_MERGE_S = 15             # такты канала и повторные события одного кулдауна в пределах 15 с — одно нажатие
SPIKE_GAP_S = 8              # пики урона ближе 8 с считаются одним
HEAL_CD_LEAD = (8.0, 3.0)    # кулдаун лекаря засчитан, если нажат за 8 с до пика … 3 с после
ADDS_LOW = 0.4               # доля кастов по аддам ниже 40% от медианы DPS

# Рейдовые кулдауны (лечение и снижение урона по рейду), актуальные для Midnight/The War Within.
# Список можно дополнить: всё, что лекарь жмёт редко (раз в 1,5+ минуты), тоже определяется по логу.
# Рейдовые кулдауны — из единой таблицы игровых данных (wcl_analyzer/data/game_data.json)
from . import game_data as _gd  # noqa: E402

RAID_CD_IDS = _gd.LazyIds(_gd.cd_ids)
RAID_CD_RE = _gd.LazyRe(_gd.cd_name_re)

# Не рейдовые кулдауны, которые лекари жмут редко и которые иначе попали бы в «нажатые рейдовые кулдауны»:
# защита на себя, перемещение и контроль, сейвы на одну цель. Названия — английские и русские (логи бывают
# на обоих языках), id — основные заклинания.
NOT_RAID_CD_IDS = {
    108271, 79206, 192077, 192063, 58875, 108287, 51490, 198103,          # шаман
    642, 498, 633, 6940, 1022, 1044, 190784, 204018,                      # паладин
    33206, 47788, 19236, 586, 73325, 32375, 10060, 47585, 8122, 121536,   # жрец
    102342, 22812, 29166, 106898, 77764, 108238, 102793, 1850, 102401,    # друид
    116849, 115203, 122783, 122278, 116844, 109132, 115008, 116841, 119381, 101643, 119996,  # монах
    357170, 363916, 374348, 358267, 370665, 374968,                       # пробудитель
}
NOT_RAID_CD_RE = re.compile(
    r"^(Astral Shift|Spiritwalker's Grace|Wind Rush Totem|Gust of Wind|Spirit Walk|Totemic Projection|Thunderstorm|"
    r"Earth Elemental|Divine Shield|Divine Protection|Lay on Hands|Blessing of (Sacrifice|Protection|Freedom|Spellwarding)|"
    r"Divine Steed|Pain Suppression|Guardian Spirit|Desperate Prayer|Fade|Leap of Faith|Mass Dispel|Power Infusion|"
    r"Dispersion|Psychic Scream|Angelic Feather|Ironbark|Barkskin|Innervate|Stampeding Roar|Renewal|Ursol's Vortex|"
    r"Dash|Wild Charge|Life Cocoon|Fortifying Brew|Diffuse Magic|Dampen Harm|Ring of Peace|Roll|Chi Torpedo|"
    r"Tiger's Lust|Leg Sweep|Transcendence.*|Time Dilation|Obsidian Scales|Renewing Blaze|Hover|Rescue|Time Spiral|"
    r"Астральный сдвиг|Благосклонность духов|Тотем ветряного порыва|Порыв ветра|Поступь духа|Гроза|"
    r"Божественный щит|Божественная защита|Возложение рук|Благословение (жертвенности|защиты|свободы|защиты от заклинаний)|"
    r"Жертвенное благословение|Божественный скакун|Подавление боли|Оберегающий дух|Отчаянная молитва|Уход в тень|"
    r"Духовное рвение|Массовое рассеивание|Придание сил|Слияние с Тьмой|Ментальный крик|Ангельское перо|Железная кора|"
    r"Дубовая кожа|Озарение|Тревожный рев|Тревожный рёв|Обновление|Тайфун|Порыв|Исцеляющий кокон|Укрепляющий отвар|"
    r"Распыление магии|Смягчение удара|Кольцо мира|Кувырок|Торпеда ци|Тигриное рвение|Круговой удар ногой|"
    r"Трансцендентность.*|Растяжение времени|Обсидиановая чешуя|Возрождающееся пламя|Парение|Спасение|Временная спираль)$",
    re.I)

ROLE_RU = {"tank": "Танк", "healer": "Лекарь", "dps": "DPS"}
ROLE_ORDER = {"tank": 0, "healer": 1, "dps": 2}
CAT_TANK, CAT_RAID, CAT_SELECT = "По танкам", "По всему рейду", "Выборочно"

# Пороги — стартовые, вынесены сюда, чтобы их было легко подстроить
ACTIVE_LOW = 0.85            # активное время ниже этого — в «Что проверить»
SELECT_RATIO = 2.0           # урон от выборочных механик ≥ 2× среднего по рейду
RAIDWIDE_RATIO = 1.35        # урон от общей механики ≥ 1,35× медианы рейда
PARSE_LOW = 25               # parse ниже 25% для DPS
WIPE_TAIL_S = 15             # смерти в последние 15 с вайпа помечаются отдельно


# ------------------------------------------------------------- загрузка
def fetch_raid_raw(client, url: str, fight=None, log=print) -> dict:
    code, url_fight, _ = parse_report_url(url)
    report = client.report(code)
    f = _pick_fight(report, fight if fight is not None else url_fight)
    fid, s, e = int(f["id"]), float(f["startTime"]), float(f["endTime"])
    log(f"Бой: {f['name']}, {'килл' if f.get('kill') else 'вайп'}, {_fmt_t((e - s) / 1000)}")
    raw = {"report": report, "fight": f}
    if f.get("phaseTransitions") and hasattr(client, "report_phases"):
        raw["phase_meta"] = client.report_phases(code)
    log("Состав рейда, урон и лечение…")
    raw["details"] = client.player_details(code, fid)
    pulls = sorted([x for x in report["fights"]
                    if x.get("encounterID") == f.get("encounterID") and x.get("difficulty") == f.get("difficulty")],
                   key=lambda x: float(x["startTime"]))
    batched = None
    if hasattr(client, "events_multi") and not getattr(client, "_no_batch", False):
        from .api import WCLError
        log("Урон, лечение, касты, смерти всех пуллов — одним запросом…")
        specs = {"taken": {"data_type": "DamageTaken"}, "deaths": {"data_type": "Deaths"},
                 "casts": {"data_type": "Casts"}, "boss_casts": {"data_type": "Casts", "hostility": "Enemies"},
                 "interrupts": {"data_type": "Interrupts"}, "dispels": {"data_type": "Dispels"},
                 "combatant": {"data_type": "CombatantInfo", "end": s + 1000}}
        bosses = boss_actor_ids(report, f)[:2]
        for i, bid in enumerate(bosses):
            specs[f"boss_debuffs_{i}"] = {"data_type": "Debuffs", "target_id": bid, "hostility": "Enemies"}
        for x in pulls:
            if int(x["id"]) != fid:
                specs[f"pull_{x['id']}"] = {"data_type": "Deaths", "fight_id": int(x["id"]),
                                            "start": float(x["startTime"]), "end": float(x["endTime"])}
        try:
            batched = client.events_multi(code, fid, s, e, specs, {"dmg_table": {"data_type": "DamageDone"},
                                                                    "heal_table": {"data_type": "Healing"}})
        except WCLError:
            client._no_batch = True
    if batched is not None:
        for k in ("taken", "deaths", "casts", "boss_casts", "interrupts", "dispels", "combatant",
                  "dmg_table", "heal_table"):
            raw[k] = batched[k]
        raw["boss_debuffs"] = [ev for i in range(len(bosses)) for ev in batched[f"boss_debuffs_{i}"]]
    else:
        raw["dmg_table"] = client.raid_table(code, fid, "DamageDone")
        raw["heal_table"] = client.raid_table(code, fid, "Healing")
        log("Полученный урон и смерти…")
        raw["taken"] = client.events(code, fid, s, e, "DamageTaken")
        raw["deaths"] = client.events(code, fid, s, e, "Deaths")
        log("Касты, прерывания, диспелы…")
        raw["casts"] = client.events(code, fid, s, e, "Casts")
        raw["boss_casts"] = client.events(code, fid, s, e, "Casts", hostility="Enemies")
        raw["interrupts"] = client.events(code, fid, s, e, "Interrupts")
        raw["dispels"] = client.events(code, fid, s, e, "Dispels")
        log("Расходники и дебаффы на боссе…")
        try:
            raw["combatant"] = client.events(code, fid, s, s + 1000, "CombatantInfo")
        except Exception:  # noqa: BLE001
            raw["combatant"] = []
        raw["boss_debuffs"] = []
        for bid in boss_actor_ids(report, f)[:2]:
            try:
                raw["boss_debuffs"] += client.events(code, fid, s, e, "Debuffs", target_id=bid, hostility="Enemies")
            except Exception:  # noqa: BLE001
                pass
    try:
        raw["rankings"] = client.report_rankings(code, fid) if f.get("kill") else None
    except Exception as ex:  # noqa: BLE001 — parse не обязателен
        raw["rankings"] = None
        log(f"Парсы игроков недоступны: {ex}")
    log(f"Пуллы на этом боссе: {len(pulls)}…")
    raw["pulls"] = []
    for x in pulls:
        if int(x["id"]) == fid:
            d = raw["deaths"]
        elif batched is not None:
            d = batched[f"pull_{x['id']}"]
        else:
            d = client.events(code, int(x["id"]), float(x["startTime"]), float(x["endTime"]), "Deaths")
        raw["pulls"].append({"fight": x, "deaths": d})
    return raw


# --------------------------------------------------------------- helpers
def _entries(t) -> list[dict]:
    if not isinstance(t, dict):
        return []
    t = t.get("data", t)
    return t.get("entries") or [] if isinstance(t, dict) else []


def _ilvl(p: dict) -> float | None:
    lo, hi = p.get("minItemLevel"), p.get("maxItemLevel")
    if lo and hi:
        return round((float(lo) + float(hi)) / 2, 1)
    return None


def _parse_ranks(r) -> dict:
    """Процентиль игроков боя по имени (и запасной ключ — номер). Разбор — общий с поиском боя."""
    from .collect import player_percentiles
    return {k: v["rank"] for k, v in player_percentiles(r).items()}


def _occurrences(times: list[float], gap: float = 3.0) -> list[float]:
    occ: list[float] = []
    for t in sorted(times):
        if not occ or t - occ[-1] > gap:
            occ.append(t)
    return occ


def _x(v: float) -> str:
    """1.84 -> «1,8» для текста по-русски."""
    return f"{v:.1f}".replace(".", ",")


def _r(x, nd=1):
    return None if x is None else round(float(x), nd)


# --------------------------------------------------------------- анализ
def fight_phases(f: dict, meta: list | None, dur: float) -> list[dict]:
    """Фазы боя: [{n, id, t, end, name, intermission}] — n — порядковый номер фазы в бою (как в MRT: p2, p3…).
    Нет переходов — пустой список (одна фаза на весь бой)."""
    f0 = float(f["startTime"])
    tr = sorted(((float(p["startTime"]) - f0) / 1000.0, int(p.get("id", 0)))
                for p in f.get("phaseTransitions") or [] if p.get("startTime") is not None)
    if not tr:
        return []
    names = {}
    for enc in meta or []:
        if not enc.get("encounterID") or int(enc["encounterID"]) == int(f.get("encounterID") or 0):
            for ph in enc.get("phases") or []:
                names[int(ph["id"])] = (ph.get("name") or "", bool(ph.get("isIntermission")))
    out = []
    for i, (t, pid) in enumerate(tr):
        name, inter = names.get(pid, ("", False))
        inter = inter or bool(re.search(r"intermission|интерм|переход", name, re.I))
        out.append({"n": i + 1, "id": pid, "t": round(max(0.0, t), 1),
                    "end": round(tr[i + 1][0] if i + 1 < len(tr) else dur, 1),
                    "name": name or (f"Интермиссия" if inter else f"Фаза {pid}"), "intermission": inter})
    return out


def _trinket_check(raw: dict):
    """(игрок, способность) → это применение надетого тринкета? Та же иконка или то же имя, что у предмета
    в слотах 13–14 (как в разборе игрока, logs.mark_trinket_spells)."""
    from .logs import gear_slots
    abil = {int(a["gameID"]): a for a in ((raw.get("report") or {}).get("masterData") or {}).get("abilities") or []}
    trink: dict = {}
    for role in ("dps", "healers", "tanks"):
        for p in (raw.get("details") or {}).get(role) or []:
            ci = p.get("combatantInfo") or {}
            ci = (ci[0] if ci else {}) if isinstance(ci, list) else ci
            if not isinstance(ci, dict):
                continue
            ts = [g for g in gear_slots(ci.get("gear") or []) if g.get("slot") in (12, 13)]
            trink[int(p.get("id", -1))] = ({g["icon"] for g in ts if g.get("icon")},
                                          {g["name"].lower() for g in ts if g.get("name")})

    def check(pid, ab) -> bool:
        icons, names = trink.get(int(pid), (set(), set()))
        a = abil.get(int(ab)) or {}
        return bool((a.get("icon") and a["icon"] in icons) or (a.get("name") and a["name"].lower() in names))
    return check


def merge_cd_ticks(raid_cds: list[dict]) -> list[dict]:
    """Канальные кулдауны (Спокойствие, Божественный гимн…) пишутся в лог каждым тактом — это одно нажатие:
    тот же кулдаун того же игрока в пределах CD_MERGE_S от первого нажатия не считаем новым."""
    first: dict = {}
    out = []
    for c in sorted(raid_cds, key=lambda c: c["t"]):
        k = (c["pid"], c["name"])
        if k in first and c["t"] - first[k] < CD_MERGE_S:
            continue
        first[k] = c["t"]
        out.append(c)
    return out


def _uniq_cds(cds: list[dict]) -> list[dict]:
    """Кулдауны в окне момента без повторов: один и тот же кулдаун одного игрока (два события, два нажатия
    подряд) — одна запись, по первому нажатию."""
    seen, out = set(), []
    for c in cds:
        k = (c.get("pid"), c.get("name"))
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def _cd_kind(c: dict) -> str:
    """raid — рейдовый кулдаун из списка (гимн, тотем, барьер, клич…); heal — кулдаун лекаря на себя или
    найденный по логу (Апофеоз, Древо Жизни, Кокон, Растяжение времени…)."""
    from .game_data import scope
    return "raid" if c.get("source") == "список" and scope(c["id"]) == "raid" else "heal"


def heaviest_mrt(heaviest: list[dict], phases: list[dict]) -> list[dict]:
    """Заметка MRT по самым тяжёлым моментам: один список по времени (со 2-й фазы — от начала фазы).
    На момент — строка рейдовых кулдаунов (время их первого нажатия) и отдельная строка кулдаунов лекарей
    со своим временем.
    Для разбора топа — готовая расстановка лучших гильдий."""
    from .raid_top import mrt_line
    # Окна соседних моментов перекрываются (8 с до момента + 5 + 3 после): одно нажатие — только в одну
    # строку, к ближайшему моменту, иначе MRT напомнит про один и тот же кулдаун дважды
    owner: dict = {}
    for hi_, h in enumerate(heaviest or []):
        for p in h.get("cd_list") or []:
            k = (p["player"], p.get("id") or p["cd"], p["t"])
            d = abs(h["t"] - p["t"])
            if k not in owner or d < owner[k][0]:
                owner[k] = (d, hi_)
    out = []
    for hi_, h in enumerate(heaviest or []):
        mine = [p for p in h.get("cd_list") or [] if owner[(p["player"], p.get("id") or p["cd"], p["t"])][1] == hi_]
        for kind in ("raid", "heal"):  # у рейдовых и у лекарей — своё время: первое нажатие своей группы
            picks = sorted([p for p in mine if p.get("kind", "raid") == kind], key=lambda p: p["t"])
            if not picks:
                continue
            t0 = min(p["t"] for p in picks)
            ph = phase_at(phases, t0)
            n = ph["n"] if ph and (ph.get("n") or 0) > 1 else None
            line = mrt_line(t0, "«" + (h["abilities"][0] if h.get("abilities") else "Пик") + "»", picks, n,
                            round(t0 - ph["t"], 1) if n else None, mech_id=h.get("ability_id"))
            if line:
                out.append({"t": t0, "time": _fmt_t(t0), "mrt": line, "kind": kind})
    return sorted(out, key=lambda x: (x["t"], x["kind"] != "raid"))


def phase_at(phases: list[dict], t: float) -> dict | None:
    cur = None
    for ph in phases or []:
        if ph["t"] <= t + 1e-6:
            cur = ph
    return cur


def guide_info_for(quoted: set, names: dict, f: dict, online: bool = True) -> dict:
    """{название из текста: {url, src: "mt"|"wowhead", video, name_ru, desc, type, todo}}.
    Сначала гайд Mythic Trap (хранится в программе), иначе — страница способности на русском Wowhead
    и её описание оттуда (online=False — без запросов, только ссылка)."""
    from .guides import find as guide_find
    by_name: dict = defaultdict(list)
    for sid, n in names.items():
        if n in quoted and sid:
            by_name[n].append(sid)
    out, missing = {}, {}
    for n in quoted:
        g = next((x for x in (guide_find(sid, None, f.get("difficulty"), f.get("name")) for sid in by_name.get(n, [])) if x), None) \
            or guide_find(None, n, f.get("difficulty"), f.get("name"))
        if g:
            out[n] = {k: v for k, v in g.items() if v not in (None, "")}
        elif by_name.get(n):
            missing[n] = by_name[n][0]
    if missing:
        from . import wowhead
        wh = wowhead.lookup(missing.values()) if online else {}
        for n, sid in missing.items():
            w = wh.get(int(sid)) or {}
            out[n] = {k: v for k, v in {"url": wowhead.page_url(sid), "src": "wowhead", "id": sid,
                                        "name_ru": w.get("name"), "desc": w.get("desc")}.items() if v}
    return out


def analyze_raid(raw: dict, avoidable: set | None = None) -> dict:
    """avoidable — id или названия механик, урон от которых считается ошибкой (spell_meta.json: _avoidable)."""
    avoidable = {str(x).lower() for x in (avoidable or set())}
    report, f = raw["report"], raw["fight"]
    code, fid = report["code"], int(f["id"])
    f0 = float(f["startTime"])
    dur = (float(f["endTime"]) - f0) / 1000
    kill = bool(f.get("kill"))
    rel = lambda ev: (float(ev["timestamp"]) - f0) / 1000  # noqa: E731
    names = {int(a["gameID"]): a["name"] for a in report["masterData"]["abilities"]}
    nm = lambda ab: names.get(int(ab), f"Spell {ab}") if ab is not None else "—"  # noqa: E731
    actors = {int(a["id"]): a for a in report["masterData"]["actors"]}

    # ------------------------------------------------------------ состав
    players: dict[int, dict] = {}
    det = raw.get("details") or {}
    for key, role in (("tanks", "tank"), ("healers", "healer"), ("dps", "dps")):
        for p in det.get(key, []) or []:
            specs = p.get("specs") or []
            spec = specs[0].get("spec") if specs and isinstance(specs[0], dict) else (specs[0] if specs else "")
            pid = int(p["id"])
            players[pid] = {"id": pid, "name": p["name"], "cls": p.get("type", ""), "spec": spec or "",
                            "role": role, "role_ru": ROLE_RU[role], "ilvl": _ilvl(p),
                            "potions": p.get("potionUse"), "healthstones": p.get("healthstoneUse")}
    if not players:
        raise LookupError("Не удалось получить состав рейда в этом бою")
    owner = lambda src: src if src in players else actors.get(src, {}).get("petOwner")  # noqa: E731

    dmg = {int(e["id"]): e for e in _entries(raw.get("dmg_table")) if "id" in e}
    heal = {int(e["id"]): e for e in _entries(raw.get("heal_table")) if "id" in e}
    ranks = _parse_ranks(raw.get("rankings"))

    # ------------------------------------------------------- касты игроков
    casts_by: dict[int, list[tuple[float, int]]] = defaultdict(list)
    cast_tgt: dict[int, Counter] = defaultdict(Counter)
    boss_actor = {aid for aid, a in actors.items() if a.get("subType") == "Boss"}
    npc_actor = {aid for aid, a in actors.items() if a.get("type") == "NPC"}
    lust = None
    pot_cast, hs_cast = Counter(), Counter()
    for ev in raw.get("casts", []):
        if ev.get("type") != "cast":
            continue
        src = owner(int(ev.get("sourceID", -1)))
        ab = int(ev.get("abilityGameID", 0))
        t = rel(ev)
        name = nm(ab)
        if (ab in LUST_IDS or LUST_RE.search(name)) and lust is None:
            lust = {"t": _r(t), "caster": players[src]["name"] if src in players else "—"}
        if src not in players:
            continue
        casts_by[src].append((t, ab))
        tg = int(ev.get("targetID", -1))
        if tg in npc_actor:
            cast_tgt[src]["boss" if tg in boss_actor else "add"] += 1
        if POTION_RE.search(name):
            pot_cast[src] += 1
        elif HEALTHSTONE_RE.search(name):
            hs_cast[src] += 1
    pot_from_details = any(p["potions"] is not None for p in players.values())

    kicks, disp = Counter(), Counter()
    for ev in raw.get("interrupts", []):
        if ev.get("type") == "interrupt" and owner(int(ev.get("sourceID", -1))) in players:
            kicks[owner(int(ev["sourceID"]))] += 1
    for ev in raw.get("dispels", []):
        if ev.get("type") == "dispel" and owner(int(ev.get("sourceID", -1))) in players:
            disp[owner(int(ev["sourceID"]))] += 1

    # ------------------------------------------------------ полученный урон
    taken: dict[int, dict[int, list]] = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))
    hits_by_ab: dict[int, list[float]] = defaultdict(list)
    hit_players: dict[int, list[tuple[float, int]]] = defaultdict(list)
    last_hits: dict[int, list[tuple[float, int, float]]] = defaultdict(list)
    for ev in raw.get("taken", []):
        if ev.get("type") != "damage":
            continue
        tgt = int(ev.get("targetID", -1))
        if tgt not in players or owner(int(ev.get("sourceID", -1))) in players:
            continue  # только по игрокам и не от своих
        a = float(ev.get("amount", 0) or 0) + float(ev.get("absorbed", 0) or 0)
        if a <= 0:
            continue
        ab, t = int(ev.get("abilityGameID", 0)), rel(ev)
        taken[tgt][ab][0] += a
        taken[tgt][ab][1] += 1
        hits_by_ab[ab].append(t)
        hit_players[ab].append((t, tgt))
        last_hits[tgt].append((t, ab, a))
    total_taken = sum(d for per in taken.values() for d, _ in per.values()) or 1.0

    tanks = {pid for pid, p in players.items() if p["role"] == "tank"}
    non_tanks = [pid for pid in players if pid not in tanks]
    boss_cast_times: dict[int, list[float]] = defaultdict(list)
    for ev in raw.get("boss_casts", []):
        if ev.get("type") == "cast":
            boss_cast_times[int(ev.get("abilityGameID", 0))].append(rel(ev))

    abilities = []
    for ab in hits_by_ab:
        per = {pid: taken[pid][ab] for pid in players if ab in taken[pid]}
        tot = sum(d for d, _ in per.values())
        if tot <= 0:
            continue
        tank_share = sum(per[pid][0] for pid in per if pid in tanks) / tot
        share_hit = len(per) / len(players)
        # Охват за одно применение: сколько разных игроков задето в каждой «волне» ударов
        groups: list[set] = []
        last_t = None
        for t, pid in sorted(hit_players[ab]):
            if last_t is None or t - last_t > 3.0:
                groups.append(set())
            groups[-1].add(pid)
            last_t = t
        coverage = (sum(len(g) for g in groups) / len(groups) / len(players)) if groups else 0.0
        cat = CAT_TANK if tank_share >= 0.7 else CAT_RAID if coverage >= 0.6 else CAT_SELECT
        nt_vals = [per.get(pid, (0.0, 0))[0] for pid in non_tanks]
        mean_nt = sum(nt_vals) / len(nt_vals) if nt_vals else 0.0
        hit_nt = [v for v in nt_vals if v > 0]
        med_nt = median(hit_nt) if hit_nt else 0.0
        occ = _occurrences(hits_by_ab[ab]) or sorted(boss_cast_times.get(ab, []))
        top = sorted(((pid, d, h) for pid, (d, h) in per.items() if pid not in tanks), key=lambda x: -x[1])[:3]
        abilities.append({
            "id": ab, "name": nm(ab), "category": cat, "total": round(tot), "share_total": tot / total_taken,
            "hits": sum(h for _, h in per.values()), "players_hit": len(per), "share_hit": share_hit,
            "coverage": coverage,
            "tank_share": tank_share, "mean_nt": mean_nt, "median_nt": med_nt, "occurrences": [_r(t) for t in occ],
            "top": [{"name": players[pid]["name"], "damage": round(d), "hits": h,
                     "ratio": (d / mean_nt) if mean_nt else None} for pid, d, h in top],
        })
    abilities.sort(key=lambda a: -a["total"])
    selective_ids = {a["id"] for a in abilities if a["category"] == CAT_SELECT}

    # ------------------------------------------------------------ смерти
    deaths = []
    first_death: dict[int, float] = {}
    for ev in sorted(raw.get("deaths", []), key=lambda e: e["timestamp"]):
        pid = int(ev.get("targetID", -1))
        if ev.get("type") != "death" or pid not in players:
            continue
        t = rel(ev)
        window = [(tt, ab, a) for tt, ab, a in last_hits[pid] if t - 5 <= tt <= t + 0.05]
        kab = ev.get("killingAbilityGameID") or (window[-1][1] if window else None)
        src = Counter()
        for _, ab, a in window:
            src[ab] += a
        before = [nm(ab) for tt, ab in casts_by[pid] if t - 10 <= tt <= t][-5:]
        first_death.setdefault(pid, t)
        deaths.append({"t": _r(t), "time": _fmt_t(t), "player": players[pid]["name"], "id": pid,
                       "role": players[pid]["role_ru"], "spec": players[pid]["spec"], "cls": players[pid]["cls"], "ability": nm(kab),
                       "sources": [{"name": nm(ab), "damage": round(v)} for ab, v in src.most_common(3)],
                       "last_casts": before, "wipe_tail": (not kill) and t >= dur - WIPE_TAIL_S})
    for i, d in enumerate(deaths):
        d["n"] = i + 1
        d["first"] = i == 0

    # ------------------------------------------------------------ игроки
    rows = []
    for pid, p in players.items():
        alive = min(dur, first_death.get(pid, dur)) or dur
        de, he = dmg.get(pid, {}), heal.get(pid, {})
        if p["ilvl"] is None and de.get("itemLevel"):
            p["ilvl"] = _r(de["itemLevel"])
        src_entry = he if p["role"] == "healer" else de
        active = (float(src_entry.get("activeTime", 0)) / 1000 / alive) if src_entry.get("activeTime") else None
        per = taken.get(pid, {})
        sel = sum(d for ab, (d, _) in per.items() if ab in selective_ids)
        top_sel = max(((ab, d, h) for ab, (d, h) in per.items() if ab in selective_ids), key=lambda x: x[1], default=None)
        rows.append({
            **p, "damage": round(float(de.get("total", 0))), "dps": float(de.get("total", 0)) / dur,
            "healing": round(float(he.get("total", 0))), "hps": float(he.get("total", 0)) / dur,
            "active": min(1.0, active) if active is not None else None,
            "parse": ranks.get(p["name"], ranks.get(pid)),
            "deaths": sum(1 for d in deaths if d["id"] == pid),
            "first_death": _r(first_death.get(pid)),
            "potions": p["potions"] if pot_from_details else pot_cast.get(pid, 0),
            "healthstones": p["healthstones"] if p["healthstones"] is not None else hs_cast.get(pid, 0),
            "interrupts": kicks.get(pid, 0), "dispels": disp.get(pid, 0),
            "taken": round(sum(d for d, _ in per.values())), "selective": round(sel),
            "top_selective": {"name": nm(top_sel[0]), "damage": round(top_sel[1]), "hits": top_sel[2]} if top_sel else None,
        })
    nt_sel = [x["selective"] for x in rows if x["role"] != "tank"]
    mean_sel = (sum(nt_sel) / len(nt_sel)) if nt_sel else 0
    for x in rows:
        x["selective_ratio"] = (x["selective"] / mean_sel) if mean_sel and x["role"] != "tank" else None
    rows.sort(key=lambda x: (x["role"] == "healer", -(x["hps"] if x["role"] == "healer" else x["dps"])))

    # ------------------------------------------- тепловая карта и выбросы
    heat_abs = [a for a in abilities if a["category"] == CAT_SELECT and a["share_total"] >= 0.01][:8]
    heat_rows = sorted([x for x in rows if x["role"] != "tank"], key=lambda x: -x["selective"])
    heatmap = {
        "abilities": [a["name"] for a in heat_abs],
        "players": [x["name"] for x in heat_rows],
        "ratio": [[(taken[x["id"]][a["id"]][0] / a["mean_nt"]) if a["mean_nt"] and a["id"] in taken[x["id"]] else 0.0
                   for a in heat_abs] for x in heat_rows],
        "damage": [[round(taken[x["id"]][a["id"]][0]) if a["id"] in taken[x["id"]] else 0 for a in heat_abs]
                   for x in heat_rows],
        "hits": [[taken[x["id"]][a["id"]][1] if a["id"] in taken[x["id"]] else 0 for a in heat_abs]
                 for x in heat_rows],
    }
    raidwide = []
    for a in abilities:
        if a["category"] != CAT_RAID or a["share_total"] < 0.03 or not a["median_nt"]:
            continue
        for pid in non_tanks:
            d = taken[pid][a["id"]][0] if a["id"] in taken[pid] else 0
            if d >= RAIDWIDE_RATIO * a["median_nt"]:
                raidwide.append({"ability": a["name"], "player": players[pid]["name"], "damage": round(d),
                                 "median": round(a["median_nt"]), "ratio": d / a["median_nt"]})
    raidwide.sort(key=lambda x: -x["ratio"])

    # ------------------------------------------------------------ пуллы
    pulls = []
    death_abs: dict[int, list] = {}
    for i, pp in enumerate(raw.get("pulls", [])):
        x = pp["fight"]
        xs = float(x["startTime"])
        xd = (float(x["endTime"]) - xs) / 1000
        pct = x.get("fightPercentage")
        if pct is not None and float(pct) > 100:
            pct = float(pct) / 100  # старый формат 0–10000
        ds = []
        for ev in sorted(pp["deaths"], key=lambda e: e["timestamp"]):
            tgt = int(ev.get("targetID", -1))
            a = actors.get(tgt, {})
            if ev.get("type") != "death" or a.get("type") in ("NPC", "Pet") or not a:
                continue
            ds.append(((float(ev["timestamp"]) - xs) / 1000, a.get("name", "?"),
                       nm(ev.get("killingAbilityGameID") or ev.get("abilityGameID"))))
            death_abs.setdefault(i, []).append(ds[-1])
        early = [d for d in ds if x.get("kill") or d[0] < xd - WIPE_TAIL_S]
        pulls.append({"n": i + 1, "fight_id": int(x["id"]), "kill": bool(x.get("kill")), "duration": _r(xd),
                      "time": _fmt_t(xd), "boss_pct": 0.0 if x.get("kill") else _r(pct),
                      "deaths": len(ds), "deaths_before_tail": len(early),
                      "first_death": ({"t": _r(ds[0][0]), "time": _fmt_t(ds[0][0]), "player": ds[0][1],
                                       "ability": ds[0][2]} if ds else None),
                      "selected": int(x["id"]) == fid})

    pull_trend = _pull_trend(pulls, death_abs)

    # ------------------------------------------------------------ таймлайн
    timeline = [{"lane": "Смерти", "t": d["t"], "label": f"{d['player']} — {d['ability']}"} for d in deaths]
    if lust:
        timeline.append({"lane": "Жажда крови", "t": lust["t"], "label": f"Жажда крови ({lust['caster']})"})
    for a in [a for a in abilities if a["category"] != CAT_TANK][:4]:
        for k, t in enumerate(a["occurrences"][:40]):
            timeline.append({"lane": a["name"], "t": t, "label": f"{a['name']} №{k + 1}"})
    lanes = ["Смерти", "Жажда крови"] + [a["name"] for a in abilities if a["category"] != CAT_TANK][:4]

    extras = _raid_extras(raw, players, rows, deaths, casts_by, cast_tgt, last_hits, taken, tanks, dur, kill,
                          rel, nm, owner, avoidable)

    # -------------------------------------------------------- что проверить
    act_vals = [x["active"] for x in rows if x["active"] is not None and x["role"] != "tank"]
    med_active = median(act_vals) if act_vals else None
    rw_by_player = defaultdict(list)
    for o in raidwide:
        rw_by_player[o["player"]].append(o)
    issues = []
    for x in rows:
        def add(sev, kind, text):
            issues.append({"player": x["name"], "id": x["id"], "role": x["role_ru"], "spec": x["spec"], "cls": x["cls"],
                           "severity": sev, "kind": kind, "text": text})
        for d in [d for d in deaths if d["id"] == x["id"]]:
            if d["wipe_tail"]:
                add(1, "Смерть", f"Смерть в {d['time']} от «{d['ability']}» в конце вайпа")
            else:
                add(5 if d["first"] else 4, "Смерть",
                    f"Смерть в {d['time']} от «{d['ability']}»" + (" — первая в бою" if d["first"] else ""))
        if x["selective_ratio"] and x["selective_ratio"] >= SELECT_RATIO and x["top_selective"]:
            ts = x["top_selective"]
            add(4, "Механики", f"Урона от выборочных механик в {_x(x['selective_ratio'])} раза больше среднего по рейду; "
                               f"больше всего — «{ts['name']}» ({_n(ts['hits'], 'попадание', 'попадания', 'попаданий')})")
        for o in rw_by_player.get(x["name"], []):
            add(3, "Механики", f"От общей механики «{o['ability']}» урона в {_x(o['ratio'])} раза больше, чем у медианы рейда")
        if (x["active"] is not None and x["role"] != "tank" and med_active is not None
                and x["active"] < min(ACTIVE_LOW, med_active - 0.05)):
            add(3, "Активность", f"Активное время {x['active']:.0%} (медиана рейда {med_active:.0%})")
        if x["role"] == "dps" and x["parse"] is not None and x["parse"] < PARSE_LOW:
            add(1, "Парс", f"Парс {x['parse']:.0f}% среди игроков своего спека")
        for kind, sev, text in extras["issues"].get(x["id"], []):
            add(sev, kind, text)
    issues.sort(key=lambda i: (-i["severity"], i["player"]))
    # Гайды Mythic Trap: способности из «Кому что поправить», у которых есть разбор, — кликабельные,
    # с роликом и кратким описанием. Нет на Mythic Trap — ссылка и описание с Wowhead (на русском).
    try:
        guide_info = guide_info_for({m for i in issues for m in re.findall(r"«([^»]+)»", i["text"])},
                                    names, f, online=not code.startswith("DEMO"))
    except Exception:  # noqa: BLE001 — ссылки на гайды необязательны
        guide_info = {}
    guide_links = {n: g["url"] for n, g in guide_info.items()}
    guide_videos = {n: g["video"] for n, g in guide_info.items() if g.get("video")}
    # Боевое зелье — в блок «Расходники на пулле», а не в «Что проверить»
    no_potion = sorted(x["name"] for x in rows if x["potions"] == 0)
    if extras.get("consumables") is None:
        extras["consumables"] = {"checked": None, "no_flask": None, "no_food": None, "no_rune": None}
    extras["consumables"]["no_potion"] = no_potion
    extras["consumables"]["potion_source"] = "playerDetails" if pot_from_details else "касты"

    # ------------------------------------------------------------ сводка
    comp = Counter(x["role"] for x in rows)
    selected_n = next((p["n"] for p in pulls if p["selected"]), None)
    real_deaths = [d for d in deaths if not d["wipe_tail"]]
    summary = {
        "raid_dps": sum(x["dps"] for x in rows), "raid_hps": sum(x["hps"] for x in rows),
        "median_active": med_active, "deaths": len(deaths), "deaths_before_tail": len(real_deaths),
        "first_death": real_deaths[0] if real_deaths else (deaths[0] if deaths else None),
        "lust": lust, "selective_total": sum(a["total"] for a in abilities if a["category"] == CAT_SELECT),
        "selective_share": sum(a["share_total"] for a in abilities if a["category"] == CAT_SELECT),
        "potions": sum(x["potions"] or 0 for x in rows), "potion_source": "playerDetails" if pot_from_details else "касты",
        "healthstones": sum(x["healthstones"] or 0 for x in rows), "interrupts": sum(kicks.values()),
        "dispels": sum(disp.values()), "pulls": len(pulls), "pull_n": selected_n,
        "kills": sum(1 for p in pulls if p["kill"]),
        "best_pct": min((p["boss_pct"] for p in pulls if p["boss_pct"] is not None), default=None),
    }
    phases = fight_phases(f, raw.get("phase_meta"), dur)
    extras["heaviest_mrt"] = heaviest_mrt(extras.get("heaviest"), phases)
    try:  # нанесение урона: героизм, бурсты DPS, окна на боссе
        from .raid_burst import burst_analysis
        extras["burst"] = burst_analysis(raw, players, owner, rel, nm, dur, phases)
    except Exception as e:  # noqa: BLE001 — дополнительная вкладка не должна ронять разбор
        extras["burst"] = {"error": str(e)}
    for sp in extras.get("spikes", []):  # фаза пика и время от её начала: следующая фаза может прийти раньше или позже
        ph = phase_at(phases, sp["t"])
        sp["phase"] = ph["n"] if ph else None
        sp["phase_t"] = round(sp["t"] - ph["t"], 1) if ph else None
    info = {"code": code, "title": report.get("title", ""), "boss": f.get("name", ""), "phases": phases,
            "difficulty": DIFFICULTY_NAMES.get(int(f.get("difficulty") or 0), str(f.get("difficulty"))),
            "kill": kill, "duration_s": _r(dur), "duration": _fmt_t(dur), "fight_id": fid,
            "boss_pct": None if kill else next((p["boss_pct"] for p in pulls if p["selected"]), None),
            "url": f"{SITE_URL}/reports/{code}#fight={fid}", "size": len(rows),
            "tanks": comp.get("tank", 0), "healers": comp.get("healer", 0), "dps": comp.get("dps", 0),
            "demo": code.startswith("DEMO"), "has_parse": bool(ranks)}
    for x in rows:
        x["dps"], x["hps"] = round(x["dps"]), round(x["hps"])
        x["active"] = _r(x["active"], 4)
        x["selective_ratio"] = _r(x["selective_ratio"], 2)
    for a in abilities:
        for k in ("share_total", "share_hit", "tank_share", "coverage"):
            a[k] = round(a[k], 4)
        a["mean_nt"], a["median_nt"] = round(a["mean_nt"]), round(a["median_nt"])
        for t in a["top"]:
            t["ratio"] = _r(t["ratio"], 2)
    heatmap["ratio"] = [[round(v, 2) for v in row] for row in heatmap["ratio"]]
    for o in raidwide:
        o["ratio"] = round(o["ratio"], 2)
    summary["raid_dps"], summary["raid_hps"] = round(summary["raid_dps"]), round(summary["raid_hps"])
    summary["median_active"] = _r(summary["median_active"], 4)
    summary["selective_share"] = round(summary["selective_share"], 4)
    return {"mode": "raid", "info": info, "summary": summary, "players": rows, "abilities": abilities,
            "heatmap": heatmap, "raidwide": raidwide, "deaths": deaths, "pulls": pulls,
            "timeline": sorted(timeline, key=lambda e: (e["lane"], e["t"])), "lanes": lanes,
            "issues": issues, "guide_links": guide_links, "guide_videos": guide_videos, "guide_info": guide_info, "extras": {**{k: v for k, v in extras.items() if k != "issues"}, "pull_trend": pull_trend},
            "brief": _raid_brief(info, summary, issues, extras, deaths, kill),
            "thresholds": {"active": ACTIVE_LOW, "select": SELECT_RATIO, "raidwide": RAIDWIDE_RATIO,
                           "parse": PARSE_LOW, "wipe_tail": WIPE_TAIL_S}}


def _pull_trend(pulls: list[dict], death_abs: dict[int, list]) -> dict:
    """Какие механики убивают рейд от пулла к пуллу (без смертей в конце вайпа)."""
    per_pull = []
    for i, p in enumerate(pulls):
        dur = p["duration"] or 0
        ds = [d for d in death_abs.get(i, []) if p["kill"] or d[0] < dur - WIPE_TAIL_S]
        per_pull.append(Counter(d[2] for d in ds))
    total = Counter()
    for c in per_pull:
        total.update(c)
    rows = []
    for name, n in total.most_common(4):
        counts = [c.get(name, 0) for c in per_pull]
        rows.append({"name": name, "counts": counts, "total": n, "pulls_with": sum(1 for x in counts if x)})
    line = None
    if rows and len(pulls) >= 2:
        r0 = rows[0]
        half = len(pulls) // 2
        first, last = sum(r0["counts"][:half]), sum(r0["counts"][half:])
        trend = ("в последних пуллах реже" if last < first else "в последних пуллах не реже" if last == first
                 else "в последних пуллах чаще")
        line = (f"Чаще всего рейд умирает от «{r0['name']}»: {_n(r0['total'], 'смерть', 'смерти', 'смертей')} "
                f"в {r0['pulls_with']} из {len(pulls)} пуллов, {trend}")
    return {"pulls": [p["n"] for p in pulls], "rows": rows, "line": line}


def run_raid(client, url: str, fight=None, log=print, avoidable: set | None = None,
             compare_top: bool = True, progress=lambda x: None, talent_data=None,
             save_talents: bool = True, mythic: bool = True) -> dict:
    raw = fetch_raid_raw(client, url, fight, log)
    R = analyze_raid(raw, avoidable)
    try:  # кулдауны состава — для плана сейвов на следующий пулл
        from .raid_cds import roster_cds
        from .talents import load_tree_data
        R["extras"]["roster_cds"] = roster_cds(raw, R, talent_data if talent_data is not None
                                               else load_tree_data(log, save=save_talents))
    except Exception as e:  # noqa: BLE001
        log(f"Кулдауны состава не определены: {e}")
    try:  # окна на боссе: описание способности — усиливает ли урон по боссу
        from .raid_burst import enrich_windows
        if "error" not in (R["extras"].get("burst") or {"error": 1}):
            enrich_windows(R["extras"]["burst"], R["info"]["duration_s"], online=not R["info"]["demo"])
    except Exception as e:  # noqa: BLE001
        log(f"Описания окон на боссе не загружены: {e}")
    if compare_top and hasattr(client, "fight_rankings"):
        from .raid_top import TOP_KILLS, brief_lines, compare_with_top, fetch_top_kills
        f = raw["fight"]
        log(f"Сравниваю с лучшими киллами этого босса (топ-{TOP_KILLS} гильдий по скорости)…")
        try:
            kills = fetch_top_kills(client, int(f["encounterID"]), int(f.get("difficulty") or 0), log=log,
                                    progress=lambda x: progress(0.5 + 0.25 * x))
            vs = compare_with_top(R, kills)
            if vs:
                from .config import DIFFICULTY_NAMES
                vs["difficulty"] = DIFFICULTY_NAMES.get(int(f.get("difficulty") or 0), "")
        except Exception as e:  # noqa: BLE001 — сравнение с топом не обязательно для разбора
            log(f"Сравнение с топ-гильдиями недоступно: {e}")
            vs = None
        R["extras"]["vs_top"] = vs
        # Вкладка «Полученный урон и сейвы»: переключатель на лучшие эпохальные киллы
        if mythic and int(f.get("difficulty") or 0) not in (0, 5):
            log("Лучшие киллы на эпохальной сложности — для вкладки «Полученный урон и сейвы»…")
            try:
                kills_m = fetch_top_kills(client, int(f["encounterID"]), 5, log=log,
                                          progress=lambda x: progress(0.75 + 0.2 * x))
                vs_m = compare_with_top(R, kills_m)
                if vs_m:
                    vs_m["difficulty"] = "эпохальный"
                R["extras"]["vs_top_alt"] = vs_m
            except Exception as e:  # noqa: BLE001
                log(f"Эпохальные киллы недоступны: {e}")
        extra = brief_lines(vs)
        try:  # нанесение урона: когда героизм и бурсты у лучших киллов
            from .raid_burst import compare_lust, compare_waves, top_summary
            B = R["extras"].get("burst") or {}
            if "error" not in B:
                B["top"] = top_summary(kills)
                lines = [x for x in (compare_lust(B.get("lust"), B["top"]), compare_waves(B.get("waves"), B["top"])) if x]
                B["hints"] = lines + list(B.get("hints") or [])
        except Exception as e:  # noqa: BLE001
            log(f"Бурсты лучших киллов не сопоставлены: {e}")
        if extra:  # строка «Пики урона по рейду…» дублирует сравнение с топом
            R["brief"] = [x for x in R["brief"] if not x.startswith("Пики урона по рейду")]
            # Сразу после строки про самый тяжёлый момент
            pos = next((i + 1 for i, line in enumerate(R["brief"]) if line.startswith("Больше всего урона")), 1)
            R["brief"] = (R["brief"][:pos] + extra + R["brief"][pos:])[:7]
    from .raid_top import _roster_lines, brief_lines as _bl, make_plan
    X = R["extras"]
    vs = X.get("vs_top")
    X["plan"] = vs["plan"] if vs else make_plan(X, {}, [], {}, R["info"].get("phases"))
    X["saves_brief"] = _bl(vs) + _roster_lines(X.get("roster_cds") or [], X["plan"])
    if X.get("vs_top_alt"):
        X["saves_brief_alt"] = _bl(X["vs_top_alt"]) + _roster_lines(X.get("roster_cds") or [], X["vs_top_alt"]["plan"])
    trend = R["extras"].get("pull_trend") or {}
    if trend.get("line") and len(R["brief"]) < 7:
        R["brief"].append(trend["line"])
    B = R["extras"].get("burst") or {}
    key = next((h for h in B.get("hints") or [] if h.startswith(("Лучшие киллы делают", "Окно «", "Героизм: у вас"))), None)
    if key:   # главное по нанесению урона — после строк про урон и сейвы, подробно во вкладке
        R["brief"] = (R["brief"][:5] + [key + " (подробно — во вкладке «Нанесение урона»)"] + R["brief"][5:])[:7]
    if hasattr(client, "points_left"):
        left = client.points_left()
        if left is not None:
            log(f"Очков API осталось в этом часе: {left:,.0f}".replace(",", " "))
    return R


# ------------------------------------------------------- дополнительно
def _raid_extras(raw, players, rows, deaths, casts_by, cast_tgt, last_hits, taken, tanks, dur, kill,
                 rel, nm, owner, avoidable) -> dict:
    issues: dict[int, list] = defaultdict(list)
    out: dict = {}

    # 1. Расходники на пулле (ауры из CombatantInfo)
    cons = {}
    for ev in raw.get("combatant") or []:
        pid = int(ev.get("sourceID", -1))
        if pid not in players:
            continue
        names = [a.get("name") or nm(a.get("ability")) for a in ev.get("auras") or []]
        cons[pid] = {"flask": any(FLASK_RE.search(n or "") for n in names),
                     "food": any(FOOD_RE.search(n or "") for n in names),
                     "rune": any(RUNE_RE.search(n or "") for n in names)}
    if cons:
        no_flask = [players[p]["name"] for p, c in cons.items() if not c["flask"]]
        no_food = [players[p]["name"] for p, c in cons.items() if not c["food"]]
        out["consumables"] = {"checked": len(cons), "no_flask": no_flask, "no_food": no_food,
                              "no_rune": [players[p]["name"] for p, c in cons.items() if not c["rune"]]}
        for pid, c in cons.items():
            miss = [w for w, k in (("настоя", "flask"), ("еды", "food")) if not c[k]]
            if miss:
                issues[pid].append(("Расходники", 2, "На пулле не было " + " и ".join(miss)))
    else:
        out["consumables"] = None

    # 2. Пики урона по рейду и рейдовые кулдауны лекарей
    per_s = defaultdict(float)
    who_s = defaultdict(set)
    for pid, hits in last_hits.items():
        if pid in tanks:
            continue
        for t, _ab, a in hits:
            per_s[int(t)] += a
            who_s[int(t)].add(pid)
    n_nt = max(1, sum(1 for p in players if p not in tanks))
    # Последние секунды вайпа (берсерк, рейд умирает) — не «пик урона», а конец попытки
    win_end = int(dur) + 1 if kill else max(5, int(dur - WIPE_TAIL_S))
    win = [sum(per_s.get(i + k, 0.0) for k in range(5)) for i in range(win_end)]
    spikes = []
    if win and max(win) > 0:
        thr = max(float(np.percentile(win, 95)), 2.5 * float(np.median([w for w in win if w > 0] or [0])),
                  0.35 * max(win))
        for i, w in enumerate(win):
            hit_n = len(set().union(*[who_s.get(i + k, set()) for k in range(5)]))
            if w >= thr and w > 0 and hit_n >= 0.4 * n_nt and (not spikes or i - spikes[-1]["t"] > SPIKE_GAP_S):
                main = Counter()
                for pid, hits in last_hits.items():
                    for t, ab, a in hits:
                        if i <= t < i + 5 and pid not in tanks:
                            main[ab] += a
                top_ab = main.most_common(1)[0][0] if main else None
                spikes.append({"t": float(i), "damage": w, "ability": nm(top_ab) if main else "—", "ability_id": top_ab})
            elif spikes and i - spikes[-1]["t"] <= SPIKE_GAP_S and w > spikes[-1]["damage"]:
                spikes[-1]["damage"] = w
    # Смерти в пик (от начала окна до 6 с после него) — признак опасного пика для плана сейвов
    for sp in spikes:
        sp["deaths"] = sum(1 for d in deaths if not d.get("wipe_tail") and sp["t"] <= d["t"] <= sp["t"] + 5 + 6)
    # Рейдовые кулдауны: известные (по ID и названию) у любого класса + эвристика для лекарей
    raid_cds = []
    used_by_others = {ab for pid, cs in casts_by.items() if players.get(pid, {}).get("role") != "healer"
                      for _, ab in cs}
    is_trinket = _trinket_check(raw)
    for pid, p in players.items():
        per_ab = defaultdict(list)
        for t, ab in casts_by[pid]:
            per_ab[ab].append(t)
        for ab, ts in per_ab.items():
            name = nm(ab)
            # не рейдовые сейвы и не боевые кулдауны лекаря: тринкеты, защита на себя, перемещение,
            # сейвы на одну цель (Кокон, Подавление боли…) — в «нажатые кулдауны» и в MRT не попадают
            if ab not in RAID_CD_IDS and (ab in NOT_RAID_CD_IDS or NOT_RAID_CD_RE.search(name) or is_trinket(pid, ab)
                                          or ab in _gd.dps_cd_ids()):   # бурсты и Придание сил — не сейвы
                continue
            known = ab in RAID_CD_IDS or _gd.name_known(name, p.get("cls", ""), p.get("spec", ""))
            if not known:
                if p["role"] != "healer" or POTION_RE.search(name) or HEALTHSTONE_RE.search(name) \
                        or LUST_RE.search(name) or ab in LUST_IDS or RACIAL_RE.match(name.strip()) \
                        or ab in used_by_others:
                    continue
                gaps = [b - a for a, b in zip(ts, ts[1:])]
                if not (len(ts) <= max(1, int(dur // 90) + 1) and (not gaps or min(gaps) >= 60)):
                    continue
            raid_cds += [{"t": t, "name": name, "player": p["name"], "role": p["role_ru"], "id": ab, "pid": pid,
                          "source": "список" if known else "по логу"} for t in ts]
    raid_cds.sort(key=lambda c: c["t"])
    raid_cds = merge_cd_ticks(raid_cds)
    lo, hi = HEAL_CD_LEAD
    order = sorted(spikes, key=lambda x: x["t"])
    nxt = {id(a): (b["t"] if b else None) for a, b in zip(order, order[1:] + [None])}
    # Одно нажатие — одному пику: если окна соседних пиков перекрываются, нажатие достаётся ближайшему
    near = {id(sp): [c for c in raid_cds if sp["t"] - lo <= c["t"] <= sp["t"] + 5 + hi] for sp in spikes}
    press_owner: dict = {}
    for sp in spikes:
        for c in near[id(sp)]:
            if id(c) not in press_owner or abs(c["t"] - sp["t"]) < abs(c["t"] - press_owner[id(c)]["t"]):
                press_owner[id(c)] = sp
    for sp in spikes:
        # Секунда самого сильного удара внутри пика — от неё считается время нажатия в плане;
        # не дальше начала следующего пика, иначе пик «съезжает» на соседа
        end = int(sp["t"]) + 5 + SPIKE_GAP_S
        if nxt[id(sp)] is not None:
            end = max(int(sp["t"]) + 1, min(end, int(nxt[id(sp)])))
        sp["peak_t"] = float(max(range(int(sp["t"]), end), key=lambda x: per_s.get(x, 0.0)))
        cov = _uniq_cds([c for c in near[id(sp)] if press_owner.get(id(c)) is sp])
        sp["covered_by"] = [f"{c['name']} ({c['player']})" for c in cov]
        sp["covered_ids"] = sorted({c["id"] for c in cov})
        sp["covered_self"] = [f"{c['name']} ({c['player']})" for c in cov if _gd.scope(c["id"]) == "self"]
        sp["k"] = 1 + sum(1 for o in spikes if o is not sp and o["t"] < sp["t"] and o.get("ability_id") == sp.get("ability_id"))
        sp["time"] = _fmt_t(sp["t"])
        sp["damage"] = round(sp["damage"])
    # Для каждого кулдауна: на какой пик он пришёлся и сколько урона рейд получил за 10 с после нажатия
    for c in raid_cds:
        hit = next((sp for sp in spikes if sp["t"] - lo <= c["t"] <= sp["t"] + 5 + hi), None)
        c["on_peak"] = hit["time"] if hit else None
        c["taken_10s"] = round(sum(per_s.get(int(c["t"]) + k, 0.0) for k in range(10)))
        c["time"] = _fmt_t(c["t"])
    out["spikes"] = spikes
    out["raid_cds"] = raid_cds
    out["heal_cds"] = [c for c in raid_cds if c["role"] == "Лекарь"]

    # Урон по рейду по 5-секундным отрезкам (без танков) и самые тяжёлые моменты
    by_ab_5 = defaultdict(Counter)
    for pid, hits in last_hits.items():
        if pid in tanks:
            continue
        for t, ab, a in hits:
            by_ab_5[int(t // 5)][ab] += a
    n5 = int(dur // 5) + 1
    timeline = []
    for b in range(n5):
        tot = sum(per_s.get(b * 5 + k, 0.0) for k in range(5))
        top = by_ab_5[b].most_common(1)
        timeline.append({"t": b * 5.0, "damage": round(tot), "ability": nm(top[0][0]) if top else None})
    out["damage_timeline"] = timeline
    total_nt = sum(x["damage"] for x in timeline) or 1
    heavy, used = [], set()
    for i in sorted(range(len(win)), key=lambda i: -win[i]):
        if len(heavy) == 5 or win[i] <= 0 or win[i] < 0.25 * max(win):
            break
        if any(abs(i - j) < 10 for j in used):
            continue
        used.add(i)
        main = Counter()
        for pid, hits in last_hits.items():
            if pid in tanks:
                continue
            for t, ab, a in hits:
                if i <= t < i + 5:
                    main[ab] += a
        cds = _uniq_cds([c for c in raid_cds if i - lo <= c["t"] <= i + 5 + hi])
        heavy.append({"t": float(i), "time": _fmt_t(i), "damage": round(win[i]), "share": round(win[i] / total_nt, 4),
                      "abilities": [nm(ab) for ab, _ in main.most_common(2)],
                      "ability_id": main.most_common(1)[0][0] if main else None,
                      "cds": [f"{c['name']} ({c['player']})" for c in cds],
                      # для заметки MRT: кто, что и когда нажал (класс — для цвета имени)
                      "cd_list": [{"cd": c["name"], "player": c["player"], "id": c["id"], "t": round(c["t"], 1),
                                   "cls": (players.get(c["pid"]) or {}).get("cls", ""), "kind": _cd_kind(c)} for c in cds]})
    out["heaviest"] = sorted(heavy, key=lambda x: -x["damage"])

    # 3. Переключение на аддов
    shares = {pid: c["add"] / (c["add"] + c["boss"]) for pid, c in cast_tgt.items()
              if pid in players and players[pid]["role"] == "dps" and (c["add"] + c["boss"]) >= 20}
    med_add = median(shares.values()) if shares else 0.0
    out["adds"] = {"median": med_add, "low": []}
    if med_add >= 0.04:
        for pid, sh in shares.items():
            if sh < ADDS_LOW * med_add:
                out["adds"]["low"].append({"name": players[pid]["name"], "share": round(sh, 3)})
                issues[pid].append(("Адды", 3, f"По аддам {sh:.0%} кастов (медиана DPS рейда — {med_add:.0%})"))

    # 4. Пропущенные прерывания: каст врага прошёл, хотя эту способность в бою прерывали
    kickable = {int(ev.get("extraAbilityGameID")) for ev in raw.get("interrupts", [])
                if ev.get("type") == "interrupt" and ev.get("extraAbilityGameID")}
    missed = Counter()
    missed_t = defaultdict(list)
    for ev in raw.get("boss_casts", []):
        ab = int(ev.get("abilityGameID", 0))
        if ev.get("type") == "cast" and ab in kickable:
            missed[ab] += 1
            missed_t[ab].append(_fmt_t(rel(ev)))
    out["missed_kicks"] = [{"name": nm(ab), "count": c, "times": missed_t[ab][:6]} for ab, c in missed.most_common()]

    # 5. Цепочка вайпа: первые смерти и что последовало
    chain = None
    real = [d for d in deaths if not d["wipe_tail"]]
    if real:
        t0 = real[0]["t"]
        after = [d for d in deaths if t0 < d["t"] <= t0 + 30]
        chain = {"steps": [f"{d['time']} {d['player']} — «{d['ability']}»" for d in real[:3]],
                 "followed_30s": len(after), "kill": kill}
    out["chain"] = chain

    # 6. Избегаемый урон по списку
    avoid = []
    if avoidable:
        for pid, per in taken.items():
            for ab, (d, h) in per.items():
                if str(ab) in avoidable or nm(ab).lower() in avoidable:
                    avoid.append({"name": players[pid]["name"], "id": pid, "ability": nm(ab),
                                  "damage": round(d), "hits": h})
        avoid.sort(key=lambda x: -x["damage"])
        for a in avoid:
            if a["hits"] >= 3:
                issues[a["id"]].append(("Избегаемый урон", 4, f"«{a['ability']}»: {_n(a['hits'], 'попадание', 'попадания', 'попаданий')}, "
                                                              f"{a['damage'] / 1e6:.1f} млн урона".replace(".", ",")))
    out["avoidable"] = avoid[:15]

    # 7. Дебаффы на боссе
    bevs, src = [], defaultdict(set)
    f0_open: dict = {}
    for ev in sorted(raw.get("boss_debuffs") or [], key=lambda e: e["timestamp"]):
        ab, typ = int(ev.get("abilityGameID", 0)), ev.get("type")
        if typ == "applydebuff":
            f0_open.setdefault(ab, rel(ev))
            s_ = owner(int(ev.get("sourceID", -1)))
            if s_ in players:
                src[ab].add(players[s_]["name"])
        elif typ == "removedebuff" and ab in f0_open:
            bevs.append((ab, f0_open.pop(ab), rel(ev)))
    for ab, t in f0_open.items():
        bevs.append((ab, t, dur))
    up = defaultdict(list)
    for ab, a, b in bevs:
        up[ab].append((a, b))
    out["boss_debuffs"] = sorted(
        [{"name": nm(ab), "uptime": round(sum(b - a for a, b in merge_intervals(iv)) / dur, 4),
          "by": sorted(src.get(ab, set()))} for ab, iv in up.items() if src.get(ab)],
        key=lambda x: -x["uptime"])[:10]
    out["issues"] = issues
    return out


def _raid_brief(info, summary, issues, extras, deaths, kill) -> list[str]:
    """3–6 строк главного по рейду; подробности — в таблицах ниже."""
    out = []
    ch = extras.get("chain")
    if ch and not kill:
        out.append("Вайп начался с: " + " → ".join(ch["steps"])
                   + (f"; за 30 с после первой смерти погибло ещё {ch['followed_30s']}" if ch["followed_30s"] else ""))
    elif ch:
        out.append(f"Первая смерть: {ch['steps'][0]}")
    hv = extras.get("heaviest") or []
    if hv:
        h = hv[0]
        mln = f"{h['damage'] / 1e6:.1f}".replace(".", ",")
        out.append(f"Больше всего урона рейд получил в {h['time']}: «{'», «'.join(h['abilities'])}» — "
                   f"{mln} млн за 5 с"
                   + (f"; кулдауны: {', '.join(h['cds'])}" if h["cds"] else "; рейдовых кулдаунов не было"))
    unc = [s for s in extras.get("spikes", []) if not s["covered_by"]]
    if extras.get("spikes"):
        out.append(f"Пики урона по рейду: {len(extras['spikes'])}, без рейдовых кулдаунов — {len(unc)}"
                   + (": " + ", ".join(f"{s['time']} «{s['ability']}»" for s in unc[:3]) if unc else ""))
    cds = extras.get("raid_cds") or []
    if cds:
        off = [c for c in cds if not c["on_peak"]]
        out.append(f"Рейдовых кулдаунов нажато: {len(cds)}"
                   + (f", вне пиков урона — {len(off)}: " + ", ".join(f"{c['name']} ({c['player']}, {c['time']})" for c in off[:3])
                      if off else ", все — на пики урона"))
    mk = extras.get("missed_kicks") or []
    if mk:
        out.append("Пропущенные прерывания: " + ", ".join(f"«{m['name']}» ×{m['count']}" for m in mk[:3]))
    av = Counter()
    for a in extras.get("avoidable") or []:
        if a["hits"] >= 3:
            av[a["name"]] += a["hits"]
    if av:
        out.append("Больше всего избегаемого урона: " + ", ".join(f"{n} ({h})" for n, h in av.most_common(3)))
    c = extras.get("consumables")
    if c and (c.get("no_flask") or c.get("no_food") or c.get("no_potion")):
        parts = []
        if c.get("no_flask"):
            parts.append(f"без настоя: {len(c['no_flask'])}")
        if c.get("no_food"):
            parts.append(f"без еды: {len(c['no_food'])}")
        if c.get("no_potion"):
            parts.append(f"без боевого зелья: {len(c['no_potion'])}")
        who = sorted(set((c.get("no_flask") or []) + (c.get("no_food") or []) + (c.get("no_potion") or [])))
        out.append("Расходники — " + ", ".join(parts) + " — " + ", ".join(who[:5]) + ("…" if len(who) > 5 else ""))
    ad = extras.get("adds") or {}
    if ad.get("low"):
        out.append("Почти не били аддов: " + ", ".join(x["name"] for x in ad["low"][:4]))
    seen: set = set()
    top = [i for i in issues if i["severity"] >= 4 and not (i["player"] in seen or seen.add(i["player"]))][:3]
    if len(out) < 4 and top:
        out += [f"{i['player']}: {i['text']}" for i in top[:4 - len(out)]]
    return out[:6]

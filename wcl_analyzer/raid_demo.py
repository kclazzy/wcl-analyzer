"""Демо рейда: синтетический отчёт на 20 игроков и 6 пуллов в формате WCL API.

FakeRaidClient отвечает теми же методами, что и WCLClient, поэтому демо проходит
ровно тот же код анализа, что и настоящий отчёт. Имена, способности и босс вымышленные.
"""
from __future__ import annotations

import random

BOSS, PET = 50, 60
CODE = "DEMORAID0001"
ENCOUNTER = 9999
MELEE, CRUSH, WAVE, SHARDS, POOL, BEAM, ENRAGE, WHISPER, FROST = 1, 900001, 900002, 900004, 900005, 900006, 900007, 900008, 900009
LUST, POT, HS, DEF, KICK, DISPEL = 2825, 431932, 6262, 800030, 800100, 800101
ADD, HYMN, TIDE, BRAND, TOUCH, RALLY = 70, 800040, 800041, 1490, 113746, 97462
FLASK, FOOD, RUNE = 431971, 462210, 453250

ROSTER = [  # имя, класс, спек, роль
    ("Гронвальд", "Warrior", "Protection", "tank"),
    ("Сайрена", "DeathKnight", "Blood", "tank"),
    ("Элария", "Priest", "Holy", "healer"),
    ("Таргун", "Shaman", "Restoration", "healer"),
    ("Вейла", "Druid", "Restoration", "healer"),
    ("Осирон", "Evoker", "Preservation", "healer"),
    ("Игрок", "Mage", "Frost", "dps"),
    ("Торвин", "Warrior", "Fury", "dps"),
    ("Мирель", "Hunter", "BeastMastery", "dps"),
    ("Кассия", "Mage", "Fire", "dps"),
    ("Дорн", "Rogue", "Assassination", "dps"),
    ("Лиана", "Priest", "Shadow", "dps"),
    ("Зарет", "Warlock", "Affliction", "dps"),
    ("Фейра", "DemonHunter", "Havoc", "dps"),
    ("Брам", "Paladin", "Retribution", "dps"),
    ("Ильвен", "Druid", "Balance", "dps"),
    ("Норра", "Shaman", "Elemental", "dps"),
    ("Квелл", "Monk", "Windwalker", "dps"),
    ("Астер", "Evoker", "Devastation", "dps"),
    ("Ровен", "DeathKnight", "Unholy", "dps"),
]
PID = {name: i + 1 for i, (name, *_ ) in enumerate(ROSTER)}

# Поведение, заложенное в демо (его должен найти анализ)
POOL_STANDERS = ("Торвин", "Мирель")        # стоят в луже
NO_DEFENSIVE = ("Лиана", "Квелл")           # без защиты на волне
LOW_ACTIVE = ("Брам",)                       # много простоя
NO_POTION = ("Ровен",)
NO_FLASK = ("Брам",)                         # без фиала на пулле
NO_FOOD = ("Брам", "Ровен")                  # без еды
NO_ADDS = ("Норра",)                         # не переключается на аддов
MISSED_KICK = "Квелл"                        # пропускает свою очередь прерывания
DEMO_AVOIDABLE = {900005}                    # «Лужа холода» — в списке избегаемых
TOP_CODES = [f"DEMOTOPKILL{i}" for i in range(1, 6)]  # «лучшие киллы» для сравнения с топ-гильдиями

# длительность, килл, HP босса в конце (%), запланированные смерти (имя, время, способность)
PULLS = [
    (95.0, False, 78.4, [("Лиана", 70.1, WAVE), ("Мирель", 82.0, POOL)]),
    (142.0, False, 61.0, [("Торвин", 48.0, POOL)]),
    (188.0, False, 43.7, [("Лиана", 160.1, WAVE)]),
    (236.0, False, 27.9, [("Мирель", 120.0, POOL), ("Дорн", 200.0, BEAM)]),
    (287.0, False, 9.6, [("Квелл", 250.1, WAVE)]),
    (331.0, True, 0.0, [("Лиана", 160.1, WAVE), ("Мирель", 281.0, POOL)]),
]

ABILITIES = {
    MELEE: "Ближний бой", CRUSH: "Сокрушение", WAVE: "Ледяная волна", SHARDS: "Осколки льда",
    POOL: "Лужа холода", BEAM: "Ледяной луч", ENRAGE: "Ярость босса", WHISPER: "Ледяной шёпот",
    FROST: "Обморожение", LUST: "Bloodlust", POT: "Зелье мощи", HS: "Камень здоровья",
    DEF: "Защитная способность", KICK: "Прерывание", DISPEL: "Рассеивание",
    HYMN: "Божественный гимн", TIDE: "Тотем целительного потока", BRAND: "Хаотическое клеймо",
    TOUCH: "Мистическое касание", RALLY: "Ободряющий клич", FLASK: "Фиал закалённой стали", FOOD: "Сытость", RUNE: "Руна усиления",
    800001: "Основной удар", 800002: "Сильный удар", 800003: "Усиление",
    800011: "Исцеление", 800012: "Быстрое исцеление", 800013: "Щит",
    800021: "Удар щитом", 800022: "Защитная стойка", 800023: "Рывок",
}
ROLE_SPELLS = {"dps": [800001, 800002, 800003], "healer": [800011, 800012, 800013],
               "tank": [800021, 800022, 800023]}


class FakeRaidClient:
    """Те же методы, что у WCLClient, но данные генерируются локально."""

    def __init__(self, seed: int = 11):
        self.seed = seed
        self.requests_made = self.cache_hits = 0
        self.base = 1_788_100_000_000
        rng = random.Random(seed)
        self.ilvl = {name: round(690 + rng.uniform(0, 8), 1) for name, *_ in ROSTER}
        self.ilvl["Брам"] = 684.0
        self.rate = {}
        for name, _, _, role in ROSTER:
            base = {"dps": 120_000, "tank": 55_000, "healer": 12_000}[role]
            self.rate[name] = base * rng.uniform(0.88, 1.08)
        self.rate["Брам"] *= 0.72
        self.rate["Игрок"] *= 0.82
        self.heal_rate = {name: (95_000 * rng.uniform(0.9, 1.1) if role == "healer" else 3_000)
                          for name, _, _, role in ROSTER}
        self.fights = []
        t = self.base + 120_000
        for i, (dur, kill, pct, _) in enumerate(PULLS):
            self.fights.append({"id": i + 1, "encounterID": ENCOUNTER, "name": "Демо-босс", "difficulty": 5,
                                "kill": kill, "startTime": t, "endTime": t + dur * 1000, "size": 20,
                                "fightPercentage": pct})
            t += dur * 1000 + 240_000
        self._cache: dict[int, dict] = {}

    # ---------------------------------------------------------------- API
    def _top_fight(self, code):
        i = int(code[-1])
        start = self.base + 50_000_000 * i
        dur = 296.0 + 7 * i
        return {"id": 1, "encounterID": ENCOUNTER, "name": "Демо-босс", "difficulty": 5, "kill": True,
                "startTime": start, "endTime": start + dur * 1000, "size": 20, "fightPercentage": 0}

    def fight_rankings(self, encounter_id, difficulty, metric="speed", page=1, max_age_s=None):
        return [{"report": {"code": c, "fightID": 1}, "guild": {"name": f"Топ-гильдия {i + 1}"},
                 "duration": (296 + 7 * (i + 1)) * 1000} for i, c in enumerate(TOP_CODES)]

    def points_left(self):
        return 3420.0

    def report(self, code):
        if code in TOP_CODES:
            rep = self.report(CODE)
            return {**rep, "code": code, "title": "ДЕМО: лучший килл", "fights": [self._top_fight(code)]}
        if code != CODE:
            raise LookupError(f"Отчёт {code} не найден")
        actors = [{"id": PID[n], "name": n, "type": c, "subType": c, "server": "Demo"} for n, c, _, _ in ROSTER]
        actors += [{"id": BOSS, "name": "Демо-босс", "type": "NPC", "subType": "Boss"},
                   {"id": ADD, "name": "Ледяной элементаль", "type": "NPC", "subType": "NPC"},
                   {"id": PET, "name": "Волк", "type": "Pet", "subType": "Pet", "petOwner": PID["Мирель"]}]
        return {"code": CODE, "title": "ДЕМО: рейдовый вечер", "startTime": self.base,
                "endTime": self.fights[-1]["endTime"] + 60_000, "zone": {"id": 53, "name": "Демо-рейд"},
                "fights": self.fights,
                "masterData": {"actors": actors,
                               "abilities": [{"gameID": k, "name": v, "type": 0} for k, v in ABILITIES.items()]}}

    def player_details(self, code, fight_id):
        g = self._gen(fight_id, code)
        out = {"tanks": [], "healers": [], "dps": []}
        for name, cls, spec, role in ROSTER:
            il = self.ilvl[name]
            out[{"tank": "tanks", "healer": "healers", "dps": "dps"}[role]].append({
                "name": name, "id": PID[name], "guid": 1000 + PID[name], "type": cls, "server": "Demo",
                "icon": f"{cls}-{spec}", "specs": [{"spec": spec, "role": role}],
                "minItemLevel": il - 2, "maxItemLevel": il + 2,
                "potionUse": g["potions"][name], "healthstoneUse": g["stones"][name]})
        return out

    def events(self, code, fight_id, start, end, data_type, source_id=None, target_id=None,
               hostility=None, include_resources=False, filter_expression=None):
        g = self._gen(fight_id, code)
        key = {"DamageTaken": "taken", "Deaths": "deaths", "Interrupts": "interrupts",
               "Dispels": "dispels", "Debuffs": "boss_debuffs", "CombatantInfo": "combatant"}.get(data_type)
        if data_type == "Casts":
            key = "boss_casts" if hostility == "Enemies" else "casts"
        evs = g.get(key, []) if key else []
        if source_id is not None:
            evs = [e for e in evs if e.get("sourceID") == source_id]
        if target_id is not None:
            evs = [e for e in evs if e.get("targetID") == target_id]
        return [e for e in evs if start <= e["timestamp"] <= end]

    def raid_table(self, code, fight_id, data_type):
        g = self._gen(fight_id, code)
        return {"data": {"totalTime": g["dur"] * 1000,
                         "entries": g["dmg_entries"] if data_type == "DamageDone" else g["heal_entries"]}}

    def report_rankings(self, code, fight_id):
        return self._gen(fight_id, code)["rankings"]

    # ---------------------------------------------------------- генерация
    def _gen(self, fight_id: int, code: str = CODE) -> dict:
        ckey = (code, int(fight_id))
        if ckey in self._cache:
            return self._cache[ckey]
        top = code in TOP_CODES  # лучший килл: все пики под кулдаунами, никто не стоит в лужах
        if top:
            f = self._top_fight(code)
            idx = 100 + int(code[-1])
            dur, kill, plan = (float(f["endTime"]) - float(f["startTime"])) / 1000, True, []
        else:
            idx = int(fight_id) - 1
            dur, kill, _pct, plan = PULLS[idx]
            f = self.fights[idx]
        rng = random.Random(self.seed * 100 + idx)
        f0 = f["startTime"]
        ts = lambda t: f0 + t * 1000.0  # noqa: E731
        fid = f["id"]

        death_at = {n: (t, ab) for n, t, ab in plan}
        if not kill:  # конец вайпа: большая часть рейда погибает в последние 10 с
            for name, *_ in ROSTER:
                if name not in death_at and rng.random() < 0.7:
                    death_at[name] = (dur - rng.uniform(0.5, 9.5), ENRAGE)
        alive = lambda n, t: t < death_at.get(n, (1e9, 0))[0]  # noqa: E731
        role = {n: r for n, _, _, r in ROSTER}
        non_tanks = [n for n, *_ in ROSTER if role[n] != "tank"]
        tanks = [n for n, *_ in ROSTER if role[n] == "tank"]

        taken, deaths, casts, boss_casts, interrupts, dispels = [], [], [], [], [], []

        def hit(t, name, ab, amount):
            if not alive(name, t) or t > dur:
                return
            taken.append({"timestamp": ts(t), "type": "damage", "sourceID": BOSS, "targetID": PID[name],
                          "abilityGameID": ab, "amount": int(amount), "absorbed": 0, "fight": fid})

        def cast(t, name, ab, src=None, target=BOSS):
            if t <= dur and (src is not None or alive(name, t)):
                casts.append({"timestamp": ts(t), "type": "cast", "sourceID": src or PID[name],
                              "targetID": target, "abilityGameID": ab, "fight": fid})

        def boss(t, ab, typ="cast"):
            if 0 <= t <= dur:
                boss_casts.append({"timestamp": ts(t), "type": typ, "sourceID": BOSS, "targetID": -1,
                                   "abilityGameID": ab, "fight": fid})

        # Смертельные удары по плану — до остальных событий, чтобы они точно были
        for name, (t, ab) in death_at.items():
            amount = {WAVE: 2_900_000, POOL: 300_000, BEAM: 950_000, ENRAGE: 5_000_000}[ab]
            taken.append({"timestamp": ts(t - 0.1), "type": "damage", "sourceID": BOSS, "targetID": PID[name],
                          "abilityGameID": ab, "amount": int(amount), "absorbed": 0, "fight": fid})
            deaths.append({"timestamp": ts(t), "type": "death", "sourceID": BOSS, "targetID": PID[name],
                           "abilityGameID": ab, "killerID": BOSS, "killingAbilityGameID": ab, "fight": fid})
            if ab == ENRAGE:
                boss(t - 0.5, ENRAGE)

        # Ледяная волна: по всему рейду; защита режет урон почти вдвое
        for tw in (70.0, 160.0, 250.0):
            if tw >= dur:
                continue
            boss(tw - 3, WAVE)
            for name, *_ in ROSTER:
                planned = death_at.get(name)
                if planned and planned[1] == WAVE and abs(planned[0] - (tw + 0.1)) < 0.05:
                    continue  # смертельный удар уже записан
                has_def = top or name not in NO_DEFENSIVE
                if has_def:
                    cast(tw - rng.uniform(1.2, 2.5), name, DEF)
                hit(tw + rng.uniform(0, 0.3), name, WAVE, 2_600_000 * (0.55 if has_def else 1.0) * rng.uniform(0.95, 1.05))
            if tw == 70.0:
                cast(tw + 2.0, "Квелл", HS)
                cast(tw + 2.5, "Дорн", HS)

        # Танки: сокрушение и ближний бой
        k = 0
        for tc in [25 + 40 * i for i in range(int(dur // 40) + 1)]:
            if tc < dur:
                boss(tc - 2, CRUSH)
                hit(tc, tanks[k % 2], CRUSH, 1_400_000 * rng.uniform(0.9, 1.1))
                k += 1
        t = 1.0
        while t < dur:
            hit(t, tanks[int(t // 80) % 2], MELEE, 180_000 * rng.uniform(0.8, 1.2))
            t += 2.0

        # Осколки: 4 случайных игрока (назначенная механика)
        for to in [12 + 20 * i for i in range(int(dur // 20) + 1)]:
            for name in rng.sample(non_tanks, 4):
                hit(to + rng.uniform(0, 1), name, SHARDS, 350_000 * rng.uniform(0.9, 1.1))

        # Лужа холода: избегаемая; двое стоят в ней регулярно
        for tp in [8 + 15 * i for i in range(int(dur // 15) + 1)]:
            if tp >= dur:
                continue
            boss(tp - 0.5, POOL)
            for name in ([] if top else POOL_STANDERS):
                for j in range(rng.randint(2, 4)):
                    hit(tp + j * 1.0 + rng.uniform(0, 0.3), name, POOL, 280_000 * rng.uniform(0.9, 1.1))
            if rng.random() < 0.5:
                other = rng.choice([n for n in non_tanks if n not in POOL_STANDERS])
                hit(tp + rng.uniform(0, 2), other, POOL, 280_000 * rng.uniform(0.9, 1.1))

        # Ледяной луч: в одного случайного игрока
        for tb in [35 + 45 * i for i in range(int(dur // 45) + 1)]:
            if tb < dur:
                boss(tb - 1.5, BEAM)
                hit(tb, rng.choice(non_tanks), BEAM, 950_000 * rng.uniform(0.9, 1.1))

        # Касты игроков: ротация, Bloodlust, зелья, прерывания, диспелы
        for name, _, _, r in ROSTER:
            t = rng.uniform(0, 1.2)
            spells = ROLE_SPELLS[r]
            while t < dur:
                on_add = (r == "dps" and 100 <= t <= 125 and name not in NO_ADDS and rng.random() < 0.8)
                cast(t, name, rng.choice(spells), target=ADD if on_add else BOSS)
                t += rng.uniform(1.3, 1.6)
                if name in LOW_ACTIVE and rng.random() < 0.22:
                    t += rng.uniform(2.0, 4.5)
        cast(1.0, "Таргун", LUST)
        # Рейдовые кулдауны лекарей: на первые две волны, третья — без них
        cast(68.0, "Элария", HYMN)
        cast(158.5, "Таргун", TIDE)
        if top:
            cast(248.0, "Элария", HYMN)    # топ закрывает и третью волну: гимн как раз откатился
        else:
            cast(200.0, "Торвин", RALLY)   # нажат между пиками — впустую
        potions, stones = {}, {}
        for name, _, _, r in ROSTER:
            n = 0 if name in NO_POTION else 1  # пре-пот: в кастах боя его не видно
            if r == "dps" and name not in NO_POTION and dur > 245:
                cast(240 + rng.uniform(0, 2), name, POT)
                n += 1
            potions[name] = n
            stones[name] = sum(1 for e in casts if e["sourceID"] == PID[name] and e["abilityGameID"] == HS)

        kickers = ["Дорн", "Фейра", "Торвин", "Квелл", None]  # None — питомец Мирель
        for i, tw in enumerate([15 + 30 * i for i in range(int(dur // 30) + 1)]):
            if tw >= dur:
                continue
            boss(tw, WHISPER, "begincast")
            who = kickers[i % len(kickers)]
            src = PET if who is None else PID[who]
            if who == MISSED_KICK:
                boss(tw + 2.0, WHISPER)  # каст прошёл: никто не прервал
            elif who is None or alive(who, tw + 1):
                interrupts.append({"timestamp": ts(tw + 1.0), "type": "interrupt", "sourceID": src,
                                   "targetID": BOSS, "abilityGameID": KICK, "extraAbilityGameID": WHISPER,
                                   "fight": fid})
        healers = [n for n, *_ in ROSTER if role[n] == "healer"]
        for i, td in enumerate([20 + 25 * i for i in range(int(dur // 25) + 1)]):
            h = healers[i % len(healers)]
            if td < dur and alive(h, td):
                dispels.append({"timestamp": ts(td), "type": "dispel", "sourceID": PID[h],
                                "targetID": PID[rng.choice(non_tanks)], "abilityGameID": DISPEL,
                                "extraAbilityGameID": FROST, "isBuff": False, "fight": fid})

        # Таблицы урона и лечения
        dmg_entries, heal_entries = [], []
        for name, cls, spec, r in ROSTER:
            alive_s = min(dur, death_at.get(name, (dur, 0))[0])
            act = 0.74 if name in LOW_ACTIVE else rng.uniform(0.9, 0.97)
            if r == "healer":
                act_d, act_h = 0.3, rng.uniform(0.93, 0.98)
            else:
                act_d, act_h = act, 0.05
            dmg_entries.append({"name": name, "id": PID[name], "guid": 1000 + PID[name], "type": cls,
                                "icon": f"{cls}-{spec}", "itemLevel": self.ilvl[name],
                                "total": int(self.rate[name] * alive_s), "activeTime": int(act_d * alive_s * 1000)})
            heal_entries.append({"name": name, "id": PID[name], "guid": 1000 + PID[name], "type": cls,
                                 "icon": f"{cls}-{spec}", "itemLevel": self.ilvl[name],
                                 "total": int(self.heal_rate[name] * alive_s), "activeTime": int(act_h * alive_s * 1000)})

        rankings = {"data": []}
        if kill:
            roles = {"tanks": {"characters": []}, "healers": {"characters": []}, "dps": {"characters": []}}
            for name, cls, spec, r in ROSTER:
                pct = rng.uniform(40, 95)
                if name == "Брам":
                    pct = 11.0
                elif name == "Игрок":
                    pct = 22.0
                elif name == "Мирель":
                    pct = 30.0
                amount = (self.heal_rate if r == "healer" else self.rate)[name]
                roles[{"tank": "tanks", "healer": "healers", "dps": "dps"}[r]]["characters"].append(
                    {"id": PID[name], "name": name, "class": cls, "spec": spec, "amount": round(amount, 1),
                     "rankPercent": round(pct, 1),
                     "bracketPercent": round(min(99.0, max(1.0, pct + rng.uniform(-15, 15))), 1)})
            rankings = {"data": [{"fightID": fid, "encounter": {"id": ENCOUNTER, "name": "Демо-босс"},
                                  "roles": roles}]}

        boss_debuffs = []
        for src_name, ab in (("Фейра", BRAND), ("Квелл", TOUCH)):
            boss_debuffs.append({"timestamp": ts(1.5), "type": "applydebuff", "sourceID": PID[src_name],
                                 "targetID": BOSS, "abilityGameID": ab, "fight": fid})
            end_t = min(dur, death_at.get(src_name, (dur, 0))[0] + 8)
            boss_debuffs.append({"timestamp": ts(end_t), "type": "removedebuff", "sourceID": PID[src_name],
                                 "targetID": BOSS, "abilityGameID": ab, "fight": fid})
        combatant = []
        for name, *_ in ROSTER:
            auras = [{"source": PID[name], "ability": RUNE, "stacks": 1, "name": ABILITIES[RUNE]}]
            if name not in NO_FLASK:
                auras.append({"source": PID[name], "ability": FLASK, "stacks": 1, "name": ABILITIES[FLASK]})
            if name not in NO_FOOD:
                auras.append({"source": PID[name], "ability": FOOD, "stacks": 1, "name": ABILITIES[FOOD]})
            combatant.append({"timestamp": ts(0), "type": "combatantinfo", "sourceID": PID[name], "auras": auras,
                              "fight": fid})

        g = {"dur": dur, "taken": sorted(taken, key=lambda e: e["timestamp"]),
             "boss_debuffs": boss_debuffs, "combatant": combatant,
             "deaths": sorted(deaths, key=lambda e: e["timestamp"]),
             "casts": sorted(casts, key=lambda e: e["timestamp"]),
             "boss_casts": sorted(boss_casts, key=lambda e: e["timestamp"]),
             "interrupts": interrupts, "dispels": dispels, "dmg_entries": dmg_entries,
             "heal_entries": heal_entries, "rankings": rankings, "potions": potions, "stones": stones}
        self._cache[ckey] = g
        return g


DEMO_URL = f"https://www.warcraftlogs.com/reports/{CODE}"

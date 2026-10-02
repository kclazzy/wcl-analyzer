"""Демо-режим: синтетические логи в формате событий WCL API.

Нужен, чтобы проверить весь конвейер (разбор событий → эталон → сравнение → Excel)
без ключа API. Способности и босс вымышленные; цифры не описывают реальный спек.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

DEMO_ENCOUNTER = 9999
BOSS_ID, SHAMAN_ID, PLAYER_ID, DH_ID, PRIEST_ID = 50, 30, 7, 31, 32
MAX_HP = 8_000_000

FB, IL, FL, GS, ORB, IV, BER, POT, AT, IB = 116, 30455, 44614, 199786, 84714, 12472, 26297, 431932, 342245, 45438
BF, FOF, WC, LUST = 190446, 44544, 228358, 2825
CRUSH, WAVE, SHARD_HIT = 900001, 900002, 900004
BRAND, CRACK, PI = 1490, 900010, 10060      # рейдовый дебафф, окно уязвимости, внешний бафф
CRACK_WINDOWS = [(118.0, 138.0), (238.0, 258.0)]

ABILITIES = {
    FB: "Ледяная стрела", IL: "Ледяное копьё", FL: "Шквал", GS: "Ледяной шип",
    ORB: "Ледяная сфера", IV: "Стылая кровь", BER: "Berserking", POT: "Зелье мощи",
    AT: "Смещение времени", IB: "Ледяная глыба", BF: "Заморозка мозгов",
    FOF: "Ледяные пальцы", WC: "Зимний холод", LUST: "Bloodlust",
    CRUSH: "Сокрушение", WAVE: "Ледяная волна", SHARD_HIT: "Осколки льда",
    BRAND: "Хаотическое клеймо", CRACK: "Трещина в броне", PI: "Придание сил",
}
BASE_DMG = {FB: 110_000, IL: 55_000, FL: 90_000, GS: 480_000, ORB: 42_000}


@dataclass
class Policy:
    reaction: float        # средняя реакция между действиями, с
    reaction_sd: float
    pause_p: float         # вероятность длинной паузы
    iv_delays: list        # задержки Стылой крови относительно готовности
    ber_times: list        # когда жать Berserking
    prepot: bool
    potion_times: list
    at_leads: list         # за сколько секунд до волны жать Смещение (None = не жать, <0 = после)
    gs_hold_p: float       # вероятность затянуть Ледяной шип (overcap осколков)
    fof_ignore_p: float    # вероятность проигнорировать прок Пальцев
    move_idle: float       # простой на волне (движение)
    raid_brand: bool = True      # в рейде есть охотник на демонов (дебафф на боссе)
    pi_times: tuple = ()         # когда жрец даёт «Придание сил»
    alt_build: bool = False      # другой набор талантов


def top_policy(rng: random.Random, dur: float) -> Policy:
    return Policy(
        reaction=abs(rng.gauss(0.07, 0.02)), reaction_sd=0.03, pause_p=0.004,
        iv_delays=[rng.uniform(0, 1.5) for _ in range(4)],
        ber_times=[0.1] + ([240 + rng.uniform(0, 1.5)] if dur > 262 else [180 + rng.uniform(0, 3)]),
        prepot=True, potion_times=[240 + rng.uniform(0.2, 2)] if dur > 262 else [],
        at_leads=[rng.uniform(1.5, 3.2) for _ in range(3)],
        gs_hold_p=0.03, fof_ignore_p=0.02, move_idle=1.0 + rng.uniform(-0.2, 0.2),
        raid_brand=rng.random() < 0.92, pi_times=(1.0, 121.0) if rng.random() < 0.7 else (),
        alt_build=rng.random() < 0.2,
    )


def my_policy(rng: random.Random) -> Policy:
    return Policy(
        reaction=0.24, reaction_sd=0.14, pause_p=0.03,
        iv_delays=[0.0, 8.0, 3.5, 0.0], ber_times=[4.0], prepot=False, potion_times=[11.0],
        at_leads=[-3.0, None, 2.5], gs_hold_p=0.45, fof_ignore_p=0.3, move_idle=1.8,
        raid_brand=False,
    )


def simulate(policy: Policy, dur: float, ilvl: float, seed: int, name: str,
             code: str, start_ms: float, trinkets: tuple[int, int],
             with_damage_events: bool = False):
    rng = random.Random(seed)
    f0 = start_ms + 60_000
    ts = lambda t: f0 + t * 1000.0  # noqa: E731
    casts, buffs, debuffs, taken, boss_casts, dmg = [], [], [], [], [], []
    boss_debuffs, resources = [], []
    dmg_total: dict[int, float] = {}
    power = 1 + 0.012 * (ilvl - 690)

    # -------------------------------------------------- механики босса
    waves = [70.0, 160.0, 250.0]
    crushes = [25.0 + 40 * k for k in range(int((dur - 25) // 40) + 1)]
    hp = [1.0]  # доля HP игрока (изменяемая в замыкании)

    def boss_cast(t, ab):
        boss_casts.append({"timestamp": ts(t), "type": "cast", "sourceID": BOSS_ID,
                           "targetID": -1, "abilityGameID": ab, "fight": 1})

    for t in crushes:
        boss_cast(t - 2.0, CRUSH)
    for t in waves:
        if t < dur:
            boss_cast(t - 3.0, WAVE)

    # -------------------------------------------------- состояние игрока
    t = 0.0
    shards, bf_until, fof, fof_until = 0, -1.0, 0, -1.0
    fof_skip = False
    wc_stacks, wc_until = 0, -1.0
    last = None
    iv_ready, orb_ready, pot_used = 0.0, 0.0, 0
    iv_until = ber_until = lust_until = pot_until = at_until = -1.0
    iv_n = 0
    ber_plan = list(policy.ber_times)
    pot_plan = list(policy.potion_times)
    at_plan = [(w - lead) if lead is not None else None for w, lead in zip(waves, policy.at_leads)]
    at_ready, at_idx = 0.0, 0
    events_hp = []

    def res():
        return {"classResources": [{"amount": shards, "max": 5, "type": 99}],
                "hitPoints": int(hp[0] * MAX_HP), "maxHitPoints": MAX_HP}

    def cast(tt, ab, begin=None):
        if begin is not None:
            casts.append({"timestamp": ts(begin), "type": "begincast", "sourceID": PLAYER_ID,
                          "targetID": BOSS_ID, "abilityGameID": ab, "fight": 1})
        casts.append({"timestamp": ts(tt), "type": "cast", "sourceID": PLAYER_ID,
                      "targetID": BOSS_ID, "abilityGameID": ab, "fight": 1, **res()})

    def buff(tt, typ, ab, src=PLAYER_ID):
        buffs.append({"timestamp": ts(tt), "type": typ, "sourceID": src, "targetID": PLAYER_ID,
                      "abilityGameID": ab, "fight": 1})

    def deal(tt, ab, base):
        mult = power * (1.3 if tt < iv_until else 1) * (1.15 if tt < ber_until else 1) \
            * (1.08 if tt < pot_until else 1) * rng.uniform(0.9, 1.1) \
            * (1.05 if policy.raid_brand else 1) * (1.15 if tt < pi_until[0] else 1) \
            * (1.25 if any(a <= tt <= b for a, b in CRACK_WINDOWS) else 1)
        amount = base * mult
        dmg_total[ab] = dmg_total.get(ab, 0.0) + amount
        if with_damage_events:
            dmg.append({"timestamp": ts(tt), "type": "damage", "sourceID": PLAYER_ID,
                        "targetID": BOSS_ID, "abilityGameID": ab, "amount": int(amount), "fight": 1})

    def haste(tt):
        h = 1.0 + (0.3 if tt < iv_until else 0) + (0.15 if tt < ber_until else 0) + (0.3 if tt < lust_until else 0)
        return h

    # Пре-пот: бафф есть на пулле, в бою видно только снятие
    if policy.prepot:
        pot_until = 30.0
        buff(30.0, "removebuff", POT)
        pot_used = 1
    # Дебаффы на боссе: рейдовый (от охотника на демонов) и окна уязвимости от самого босса
    def bdeb(tt, typ, ab, src):
        boss_debuffs.append({"timestamp": ts(tt), "type": typ, "sourceID": src, "targetID": BOSS_ID,
                             "abilityGameID": ab, "fight": 1})
    if policy.raid_brand:
        bdeb(2.0, "applydebuff", BRAND, DH_ID)
        bdeb(dur, "removedebuff", BRAND, DH_ID)
    for a, b in CRACK_WINDOWS:
        if a < dur:
            bdeb(a, "applydebuff", CRACK, BOSS_ID)
            bdeb(min(b, dur), "removedebuff", CRACK, BOSS_ID)
    pi_until = [-1.0]
    for tp in policy.pi_times:
        if tp < dur:
            buff(tp, "applybuff", PI, PRIEST_ID)
            buff(tp + 15, "removebuff", PI, PRIEST_ID)
    pi_iv = [(tp, tp + 15) for tp in policy.pi_times]

    # Bloodlust от шамана на 1-й секунде
    lust_until = 41.0
    buff(1.0, "applybuff", LUST, SHAMAN_ID)
    buff(41.0, "removebuff", LUST, SHAMAN_ID)

    wave_idx = 0
    while t < dur - 0.5:
        pi_until[0] = next((b for a, b in pi_iv if a <= t <= b), -1.0)
        # движение на волне
        if wave_idx < len(waves) and t >= waves[wave_idx]:
            t = max(t, waves[wave_idx]) + policy.move_idle
            wave_idx += 1
        # истечение проков
        if bf_until > 0 and t > bf_until:
            buff(bf_until, "removebuff", BF)
            bf_until = -1.0
        if fof and t > fof_until:
            buff(fof_until, "removebuff", FOF)
            fof, fof_skip = 0, False
        if wc_stacks and t > wc_until:
            debuffs.append({"timestamp": ts(wc_until), "type": "removedebuff", "sourceID": PLAYER_ID,
                            "targetID": BOSS_ID, "abilityGameID": WC, "fight": 1})
            wc_stacks = 0

        gcd = max(0.75, 1.2 / haste(t))
        # off-GCD: Berserking, зелье, Смещение времени
        if ber_plan and t >= ber_plan[0]:
            cast(t, BER)
            buff(t, "applybuff", BER)
            buff(t + 12, "removebuff", BER)
            ber_until = t + 12
            ber_plan.pop(0)
        if pot_plan and t >= pot_plan[0]:
            cast(t, POT)
            buff(t, "applybuff", POT)
            buff(t + 30, "removebuff", POT)
            pot_until = t + 30
            pot_plan.pop(0)
        while at_idx < len(at_plan) and (at_plan[at_idx] is None or at_plan[at_idx] < t - 20):
            at_idx += 1
        if at_idx < len(at_plan) and at_plan[at_idx] is not None and t >= at_plan[at_idx] and t >= at_ready:
            cast(t, AT)
            buff(t, "applybuff", AT)
            buff(t + 10, "removebuff", AT)
            at_until, at_ready = t + 10, t + 60
            at_idx += 1

        # выбор действия
        ab, cast_time = None, 0.0
        if t >= iv_ready + policy.iv_delays[min(iv_n, len(policy.iv_delays) - 1)]:
            ab = IV
        elif t >= orb_ready:
            ab = ORB
        elif bf_until > t and last == FB:
            ab = FL
        elif wc_stacks:
            ab = IL
        elif shards >= 5 and rng.random() > policy.gs_hold_p:
            ab, cast_time = GS, 2.4
        elif fof and not fof_skip:
            ab = IL
        else:
            ab, cast_time = FB, 1.6
        cast_time /= haste(t)
        begin = t if cast_time else None
        t_done = t + cast_time
        cast(t_done, ab, begin)

        if ab == IV:
            iv_until, iv_ready = t_done + 20, t_done + 120
            iv_n += 1
            buff(t_done, "applybuff", IV)
            buff(t_done + 20, "removebuff", IV)
        elif ab == ORB:
            orb_ready = t_done + 60
            for k in range(10):
                if t_done + 1 + k < dur:
                    deal(t_done + 1 + k, ORB, BASE_DMG[ORB])
            if rng.random() < 0.6:
                if fof >= 2:
                    buff(t_done, "refreshbuff", FOF)
                else:
                    buff(t_done, "applybuff" if fof == 0 else "applybuffstack", FOF)
                    fof += 1
                fof_until = t_done + 15
                fof_skip = fof_skip or rng.random() < policy.fof_ignore_p
        elif ab == FL:
            deal(t_done, FL, BASE_DMG[FL])
            buff(t_done, "removebuff", BF)
            bf_until = -1.0
            debuffs.append({"timestamp": ts(t_done), "type": "applydebuff", "sourceID": PLAYER_ID,
                            "targetID": BOSS_ID, "abilityGameID": WC, "fight": 1})
            wc_stacks, wc_until = 2, t_done + 6
        elif ab == IL:
            boosted = wc_stacks > 0 or fof > 0
            deal(t_done, IL, BASE_DMG[IL] * (3 if boosted else 1))
            if wc_stacks:
                wc_stacks -= 1
                debuffs.append({"timestamp": ts(t_done), "type": "removedebuff" if wc_stacks == 0 else "removedebuffstack",
                                "sourceID": PLAYER_ID, "targetID": BOSS_ID, "abilityGameID": WC, "fight": 1})
            elif fof:
                fof -= 1
                buff(t_done, "removebuff" if fof == 0 else "removebuffstack", FOF)
                if fof == 0:
                    fof_skip = False
        elif ab == GS:
            deal(t_done, GS, BASE_DMG[GS])
            shards = 0
        elif ab == FB:
            deal(t_done, FB, BASE_DMG[FB])
            resources.append({"timestamp": ts(t_done), "type": "resourcechange", "sourceID": PLAYER_ID,
                              "targetID": PLAYER_ID, "resourceChange": 1, "resourceChangeType": 99,
                              "waste": 1 if shards >= 5 else 0, "fight": 1})
            shards = min(5, shards + 1)
            if rng.random() < 0.25:
                if bf_until > t_done:
                    buff(t_done, "refreshbuff", BF)
                else:
                    buff(t_done, "applybuff", BF)
                bf_until = t_done + 15
            if rng.random() < 0.15:
                if fof >= 2:
                    buff(t_done, "refreshbuff", FOF)
                else:
                    buff(t_done, "applybuff" if fof == 0 else "applybuffstack", FOF)
                    fof += 1
                fof_until = t_done + 15
                fof_skip = fof_skip or rng.random() < policy.fof_ignore_p
        last = ab
        react = abs(rng.gauss(policy.reaction, policy.reaction_sd))
        if rng.random() < policy.pause_p:
            react += rng.uniform(1.5, 3.5)
        t = t_done + (gcd if cast_time == 0 else max(0.0, gcd - cast_time)) + react

    # Закрываем открытые эффекты
    if bf_until > 0:
        buff(min(dur, bf_until), "removebuff", BF)
    if fof:
        buff(min(dur, fof_until), "removebuff", FOF)

    # -------------------------------------------------- входящий урон
    hits = [(tc, CRUSH, 1_200_000) for tc in crushes if tc < dur]
    hits += [(tw, WAVE, 5_400_000) for tw in waves if tw < dur]
    tt = 3.0
    while tt < dur:
        hits.append((tt, SHARD_HIT, 150_000 * rng.uniform(0.7, 1.3)))
        tt += rng.uniform(5, 9)
    hits.sort()
    cur, last_t = 1.0, 0.0
    at_windows = [(b["timestamp"] - f0) / 1000 for b in buffs if b["abilityGameID"] == AT and b["type"] == "applybuff"]
    for th, ab, amount in hits:
        cur = min(1.0, cur + (th - last_t) * 0.12)
        last_t = th
        if any(s <= th <= s + 10 for s in at_windows):
            amount *= 0.4
        cur = max(0.05, cur - amount / MAX_HP)
        taken.append({"timestamp": ts(th), "type": "damage", "sourceID": BOSS_ID, "targetID": PLAYER_ID,
                      "abilityGameID": ab, "amount": int(amount), "absorbed": 0,
                      "hitPoints": int(cur * MAX_HP), "maxHitPoints": MAX_HP, "fight": 1})

    # HP игрока на кастах — по последнему входящему удару
    hp_track = [((e["timestamp"] - f0) / 1000, e["hitPoints"]) for e in taken]
    for c in casts:
        tc = (c["timestamp"] - f0) / 1000
        prev = [h for th, h in hp_track if th <= tc]
        if "hitPoints" in c and prev:
            c["hitPoints"] = min(MAX_HP, int(prev[-1] + (tc - [th for th, _ in hp_track if th <= tc][-1]) * 0.12 * MAX_HP))

    # 0–9 — дерево класса, 10–39 — спека, 40–43 / 44–47 — две героические ветки
    talents = (list(range(40)) if not policy.alt_build else list(range(8, 40))) + \
              (list(range(40, 44)) if not policy.alt_build else list(range(44, 48)))
    gear = [{"id": 200000 + i, "itemLevel": round(ilvl + rng.uniform(-3, 3))} for i in range(16)]
    gear[3] = {"id": 0, "itemLevel": 1}
    gear[12] = {"id": trinkets[0], "itemLevel": round(ilvl)}
    gear[13] = {"id": trinkets[1], "itemLevel": round(ilvl)}
    raw = {
        "casts": casts, "buffs": buffs, "debuffs": debuffs, "dmg_taken": taken,
        "boss_debuffs": debuffs + boss_debuffs, "resources": resources,
        "deaths": [], "boss_casts": boss_casts, "dmg_done": dmg,
        "combatant": [{"timestamp": ts(0), "type": "combatantinfo", "sourceID": PLAYER_ID, "gear": gear,
                       "talentTree": [{"id": 1000 + k, "nodeID": 5000 + k, "rank": 1} for k in talents]}],
        "dmg_table": {"totalTime": dur * 1000,
                      "entries": [{"name": ABILITIES.get(k, str(k)), "guid": k, "total": v}
                                  for k, v in dmg_total.items()]},
    }
    report = {
        "code": code, "title": "ДЕМО", "startTime": start_ms, "endTime": ts(dur) + 60_000,
        "zone": {"id": 53, "name": "Демо-рейд"},
        "fights": [{"id": 1, "encounterID": DEMO_ENCOUNTER, "name": "Демо-босс", "difficulty": 5,
                    "kill": True, "startTime": f0, "endTime": ts(dur),
                    "enemyNPCs": [{"id": BOSS_ID, "gameID": 999}],
                    "phaseTransitions": [{"id": 1, "startTime": f0}, {"id": 2, "startTime": ts(150.0)}]}],
        "masterData": {
            "actors": [{"id": PLAYER_ID, "name": name, "type": "Mage", "subType": "Mage", "server": "Demo"},
                       {"id": BOSS_ID, "name": "Демо-босс", "type": "NPC", "subType": "Boss"},
                       {"id": SHAMAN_ID, "name": "Шаман", "type": "Shaman", "subType": "Shaman"},
                       {"id": DH_ID, "name": "Охотник", "type": "DemonHunter", "subType": "DemonHunter"},
                       {"id": PRIEST_ID, "name": "Жрец", "type": "Priest", "subType": "Priest"}],
            "abilities": [{"gameID": k, "name": v, "type": 0} for k, v in ABILITIES.items()],
        },
    }
    return report, report["fights"][0], report["masterData"]["actors"][0], raw


def demo_logs(n_top: int = 25, seed: int = 42):
    """Возвращает (top_logs, my_log) — PlayerLog, собранные тем же кодом, что и для API."""
    from .logs import build_player_log

    rng = random.Random(seed)
    base_ms = 1_788_000_000_000  # сентябрь 2026
    tops = []
    for i in range(n_top):
        dur = rng.uniform(288, 332)
        ilvl = rng.uniform(691, 699)
        pol = top_policy(rng, dur)
        rep, fight, actor, raw = simulate(pol, dur, ilvl, seed + i + 1, f"Топ{i + 1:02d}",
                                          f"DEMOTOP{i + 1:02d}xx", base_ms - rng.uniform(0, 12) * 86400000,
                                          (219314, 225649 if rng.random() < 0.7 else 219312))
        tops.append(build_player_log(rep, fight, actor, raw, spec="Frost", cls="Mage"))
    tops.sort(key=lambda log: -log.dps)
    for k, log in enumerate(tops):
        log.rank = k + 1
    rep, fight, actor, raw = simulate(my_policy(rng), 305.0, 689.5, seed + 999, "Игрок",
                                      "DEMOMYLOGxx", base_ms, (219314, 212456), with_damage_events=True)
    me = build_player_log(rep, fight, actor, raw, spec="Frost", cls="Mage")
    # Демо-отличия в талантах: в узле выбора 5010 — другой вариант, 5003 не взят
    tt = {nd: (e, r) for nd, e, r in me.talent_tree}
    tt.pop(5003, None)
    tt.pop(5042, None)  # и в героической ветке один талант не взят
    if 5010 in tt:
        tt[5010] = (9010, 1)
    me.talent_tree = sorted((nd, e, r) for nd, (e, r) in tt.items())
    return tops, me


def _die(log, frac: float):
    """Обрезает лог смертью на доле боя frac (для демо)."""
    td = log.duration * frac
    log.casts = [c for c in log.casts if c.t <= td]
    log.deaths = [td]
    log.dmg_taken = [h for h in log.dmg_taken if h[0] <= td]
    log.buff_events = [e for e in log.buff_events if e[0] <= td]
    log.buffs = {k: [(a, min(b, td)) for a, b in iv if a <= td] for k, iv in log.buffs.items()}
    log.debuffs = {k: [(a, min(b, td)) for a, b in iv if a <= td] for k, iv in log.debuffs.items()}
    log.dmg_by_ability = {k: v * frac for k, v in log.dmg_by_ability.items()}
    log.dmg_timeline = [x for x in log.dmg_timeline if x[0] <= td]
    log.dps *= frac
    log.dps_calc *= frac
    return log


def demo_raid_players(n_top: int = 25, seed: int = 42):
    """Эталон и пять DPS одного рейда (демо: все — Frost Mage, чтобы хватило одного эталона)."""
    from .logs import build_player_log
    tops, me = demo_logs(n_top, seed)
    rng = random.Random(seed + 500)
    base_ms = 1_788_000_000_000

    def mid(reaction, gs_hold, fof_ignore, prepot=True, delays=(0, 3, 2, 0)):
        return Policy(reaction=reaction, reaction_sd=0.06, pause_p=0.012, iv_delays=list(delays),
                      ber_times=[0.1, 240.5], prepot=prepot, potion_times=[241.0] if prepot else [12.0],
                      at_leads=[2.0, 2.5, 2.2], gs_hold_p=gs_hold, fof_ignore_p=fof_ignore, move_idle=1.3,
                      raid_brand=False)

    plans = [("Кассия", top_policy(rng, 305.0), 696.0, None),
             ("Астер", mid(0.13, 0.15, 0.08), 693.0, None),
             ("Зарет", mid(0.16, 0.3, 0.35, prepot=False, delays=(0, 9, 6, 0)), 691.0, None),
             ("Ильвен", top_policy(rng, 305.0), 695.0, 0.55)]
    players = [{"name": me.name, "cls": me.cls, "spec": me.spec, "id": me.actor_id, "log": me}]
    for k, (name, pol, ilvl, die) in enumerate(plans):
        pol.raid_brand = False
        rep, fight, actor, raw = simulate(pol, 305.0, ilvl, seed + 2000 + k, name, "DEMOMYLOGxx", base_ms,
                                          (219314, 225649), with_damage_events=True)
        log = build_player_log(rep, fight, actor, raw, spec="Frost", cls="Mage")
        log.actor_id = 100 + k
        if die:
            _die(log, die)
        players.append({"name": name, "cls": "Mage", "spec": "Frost", "id": log.actor_id, "log": log})
    return tops, players

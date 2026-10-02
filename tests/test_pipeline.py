"""Сквозной тест без сети: фейковый клиент отвечает событиями в формате WCL API.

Запуск: python tests/test_pipeline.py
"""
from __future__ import annotations

import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wcl_analyzer.collect import collect_reference, load_my_log  # noqa: E402
from wcl_analyzer.compare import compare  # noqa: E402
from wcl_analyzer.demo import my_policy, simulate, top_policy  # noqa: E402
from wcl_analyzer.excel_report import write_compare_workbook  # noqa: E402
from wcl_analyzer.logs import parse_report_url  # noqa: E402
from wcl_analyzer.reference import build_reference  # noqa: E402


class FakeClient:
    """Минимальная замена WCLClient: те же методы, данные из демо-симуляции."""

    def __init__(self):
        self.reports, self.raws, self.ranks = {}, {}, []
        self.requests_made = self.cache_hits = 0
        rng = random.Random(7)
        for i in range(30):
            dur = rng.uniform(250, 360)
            code = f"TOPREPORT{i:03d}"
            rep, fight, actor, raw = simulate(top_policy(rng, dur), dur, rng.uniform(691, 699), 100 + i,
                                              f"Top{i}", code, 1_788_000_000_000, (1, 2))
            self.reports[code], self.raws[code] = rep, raw
            total = sum(e["total"] for e in raw["dmg_table"]["entries"])
            self.ranks.append({"name": f"Top{i}", "amount": total / dur, "duration": dur * 1000,
                               "report": {"code": code, "fightID": 1, "startTime": 0}})
        self.ranks.sort(key=lambda r: -r["amount"])
        rep, fight, actor, raw = simulate(my_policy(rng), 305.0, 690, 999, "Me", "MYREPORT0001",
                                          1_788_000_000_000, (1, 3), with_damage_events=True)
        self.reports["MYREPORT0001"], self.raws["MYREPORT0001"] = rep, raw

    def report(self, code):
        return self.reports[code]

    def player_details(self, code, fight_id):
        a = self.reports[code]["masterData"]["actors"][0]
        return {"dps": [{"name": a["name"], "id": a["id"], "type": "Mage", "specs": [{"spec": "Frost"}]}]}

    def events(self, code, fight_id, start, end, data_type, source_id=None, target_id=None,
               hostility=None, include_resources=False):
        raw = self.raws[code]
        key = {"Casts": "boss_casts" if hostility == "Enemies" else "casts", "Buffs": "buffs",
               "Debuffs": "debuffs", "DamageTaken": "dmg_taken", "Deaths": "deaths",
               "CombatantInfo": "combatant", "DamageDone": "dmg_done"}[data_type]
        return raw.get(key, [])

    def damage_table(self, code, fight_id, source_id):
        return self.raws[code]["dmg_table"]

    def rankings(self, encounter_id, class_name, spec_name, difficulty, page=1, force=False, max_age_s=None):
        import time
        if force or not getattr(self, "_ranks_at", None):
            self._ranks_at = time.time()
        return {"name": "Демо-босс", "_fetched_at": self._ranks_at,
                "characterRankings": {"rankings": self.ranks[(page - 1) * 100: page * 100], "hasMorePages": False}}


def test_refresh():
    """Эталон сохраняется в список и обновляется принудительно."""
    import os
    import time
    from wcl_analyzer import settings
    from wcl_analyzer.api import Cache
    from wcl_analyzer.collect import refresh_refs
    tmp = Path(tempfile.mkdtemp())
    old = os.getcwd()
    os.chdir(tmp)
    try:
        client = FakeClient()
        client.cache = Cache(tmp / "wcl_cache.sqlite")
        collect_reference(client, 9999, "Mage", "Frost", 5, top_n=10, duration=300, log=lambda *_: None)
        refs = settings.list_refs(tmp / "wcl_cache.sqlite")
        assert len(refs) == 1 and refs[0]["n_logs"] == 10 and not refs[0]["stale"]
        first = refs[0]["collected_at"]
        time.sleep(0.05)
        assert refresh_refs(client, log=lambda *_: None) == 1
        assert settings.list_refs(tmp / "wcl_cache.sqlite")[0]["collected_at"] > first
        settings.save_user_prefs(tmp / "wcl_cache.sqlite", "local", ref_max_age_days=1)
        assert settings.user_prefs(tmp / "wcl_cache.sqlite", "local")["ref_max_age_days"] == 1
        assert settings.list_refs(tmp / "wcl_cache.sqlite", "someone-else") == []
        assert settings.load()["token"]
        print("OK эталоны: сохранение и обновление")
    finally:
        os.chdir(old)


def main():
    assert parse_report_url("https://www.warcraftlogs.com/reports/AbCd1234EfGh#fight=12&source=5") == \
        ("AbCd1234EfGh", "12", 5)
    assert parse_report_url("https://www.warcraftlogs.com/reports/AbCd1234EfGh") == ("AbCd1234EfGh", None, None)
    assert parse_report_url("AbCd1234EfGh")[0] == "AbCd1234EfGh"

    client = FakeClient()
    me = load_my_log(client, "https://www.warcraftlogs.com/reports/MYREPORT0001#fight=last&source=7")
    assert me.name == "Me" and me.spec == "Frost" and me.cls == "Mage"
    assert me.casts and me.dmg_timeline and me.ilvl

    tops, label = collect_reference(client, me.encounter_id, me.cls, me.spec, me.difficulty,
                                    top_n=25, duration=me.duration, log=lambda *_: None)
    assert "±" in label, label
    assert all(abs(t.duration - me.duration) / me.duration <= 0.2 for t in tops)

    ref = build_reference(tops, label=label)
    r = compare(me, ref)
    sections = {f.section for f in r.findings}
    for expected in ("Простой", "Кулдауны", "Бурст", "Защита", "Проки", "Зелья"):
        assert expected in sections, (expected, sections)
    assert len(r.top5) == 5 and r.plan
    out = Path(tempfile.gettempdir()) / "wcl_test_compare.xlsx"
    write_compare_workbook(r, out)
    assert out.exists() and out.stat().st_size > 20_000
    print(f"OK: {len(tops)} эталонных логов ({label}), {len(r.findings)} отличий, "
          f"надёжность {r.reliability['overall']}, отчёт {out}")


if __name__ == "__main__":
    main()


def test_raid():
    from wcl_analyzer.excel_raid import write_raid_workbook
    from wcl_analyzer.raid import run_raid
    from wcl_analyzer.raid_demo import CODE, DEMO_URL, FakeRaidClient

    R = run_raid(FakeRaidClient(), DEMO_URL, log=lambda *_: None, talent_data=[])
    Xs = R["extras"]
    assert Xs["plan"] and all(r["picks"] for r in Xs["plan"]), "во вкладке «Полученный урон и сейвы» нет плана"
    assert Xs["roster_cds"] and Xs["saves_brief"], "нет кулдаунов состава или выжимки сейвов"
    # пик со смертью — опасный: несколько сейвов разных игроков; у каждого пика — запасные, уже откатанные
    danger = [r for r in Xs["plan"] if r["deaths"]]
    assert danger and all(len(r["picks"]) >= 2 and len({p["player"] for p in r["picks"]}) == len(r["picks"]) for r in danger)
    assert all(r["spare"] for r in Xs["plan"]), "нет запасных вариантов"
    cdmap = {(c["player"], c["name"]): c["cd"] for c in Xs["roster_cds"]}
    uses = {}
    for r in Xs["plan"]:
        for p in r["picks"]:
            uses.setdefault((p["player"], p["cd"]), []).append(r["t"])
    for k, ts in uses.items():
        assert all(b - a >= cdmap[k] - 0.01 for a, b in zip(sorted(ts), sorted(ts)[1:])), (k, ts)
    assert R["info"]["size"] == 20 and R["info"]["kill"]
    cats = {a["name"]: a["category"] for a in R["abilities"]}
    assert cats["Ледяная волна"] == "По всему рейду" and cats["Лужа холода"] == "Выборочно"
    assert cats["Сокрушение"] == "По танкам" and cats["Осколки льда"] == "Выборочно"
    texts = {(i["player"], i["kind"]) for i in R["issues"]}
    for expected in (("Лиана", "Смерть"), ("Мирель", "Смерть"), ("Торвин", "Механики"), ("Мирель", "Механики"),
                     ("Брам", "Активность"), ("Квелл", "Механики"), ("Ровен", "Зелья")):
        assert expected in texts, expected
    assert R["deaths"][0]["player"] == "Лиана" and R["deaths"][0]["first"]
    assert len(R["pulls"]) == 6 and R["pulls"][-1]["kill"] and R["pulls"][0]["boss_pct"] == 78.4
    out = Path(tempfile.gettempdir()) / "wcl_test_raid.xlsx"
    write_raid_workbook(R, out)
    assert out.exists()
    print(f"OK рейд: {len(R['players'])} игроков, {len(R['issues'])} замечаний, {len(R['pulls'])} пуллов")


if __name__ == "__main__":
    test_raid()
    test_refresh()


def test_analysis_quality():
    """Смерть не порождает ложных находок; топ против остальных почти без находок;
    дебаффы на боссе и выжимка на месте."""
    import copy
    from wcl_analyzer.compare import compare
    from wcl_analyzer.demo import demo_logs
    from wcl_analyzer.raid import run_raid
    from wcl_analyzer.raid_demo import DEMO_AVOIDABLE, DEMO_URL, FakeRaidClient
    from wcl_analyzer.reference import build_reference

    tops, me = demo_logs()
    # 1. Топ, умерший на середине боя
    v = copy.deepcopy(tops[0])
    td = v.duration / 2
    v.casts = [c for c in v.casts if c.t <= td]
    v.deaths = [td]
    v.dmg_taken = [h for h in v.dmg_taken if h[0] <= td]
    v.buff_events = [e for e in v.buff_events if e[0] <= td]
    v.buffs = {k: [(a, min(b, td)) for a, b in iv if a <= td] for k, iv in v.buffs.items()}
    v.debuffs = {k: [(a, min(b, td)) for a, b in iv if a <= td] for k, iv in v.debuffs.items()}
    v.dps /= 2
    r = compare(v, build_reference(tops[1:]))
    high = [f for f in r.findings if f.impact_level == "HIGH" and not f.noise]
    assert [f.section for f in high] == ["Смерти"], [f.title for f in high]

    # 2. Шумовой порог: топ против остальных — суммарно меньше 2% «потерь»
    for i in (0, 7, 14):
        rr = compare(tops[i], build_reference([t for j, t in enumerate(tops) if j != i]))
        tot = sum(f.impact or 0 for f in rr.findings if not f.noise and not f.context)
        assert tot < 0.02, (i, tot)

    # 3. Мой лог: дебаффы на боссе, внешние баффы, выжимка
    ref = build_reference(tops, me=me)
    r = compare(me, ref)
    keys = {f.key for f in r.findings}
    assert "raiddebuff:1490" in keys and "ext:10060" in keys, keys
    assert any(k.startswith("vuln:") for k in keys), keys
    assert any(k.startswith("prio:") for k in keys) and "res:waste" in keys, keys
    assert 1 <= len(r.brief["actions"]) <= 3 and r.brief["context"], r.brief
    assert all(not f.context for f in r.top5)
    assert ref.build_note, "фильтр по билду не сработал"

    # 4. Рейд: выжимка и дополнительные проверки
    R = run_raid(FakeRaidClient(), DEMO_URL, None, log=lambda m: None, avoidable={str(x) for x in DEMO_AVOIDABLE})
    X = R["extras"]
    assert 2 <= len(R["brief"]) <= 7, R["brief"]
    assert X["missed_kicks"] and X["consumables"]["no_flask"] == ["Брам"], X["consumables"]
    assert [a["name"] for a in X["adds"]["low"]] == ["Норра"], X["adds"]
    assert any(not s["covered_by"] for s in X["spikes"]) and X["avoidable"]
    assert X["heaviest"][0]["abilities"][0] == "Ледяная волна", X["heaviest"][0]
    names = {c["name"]: c for c in X["raid_cds"]}
    assert {"Божественный гимн", "Тотем целительного потока", "Ободряющий клич"} <= set(names), names
    assert names["Ободряющий клич"]["on_peak"] is None and names["Божественный гимн"]["on_peak"]
    assert len(X["damage_timeline"]) >= 60
    V = X["vs_top"]
    assert V and V["n"] == 5 and V["top_cover"] == 1.0 and abs(V["my_cover"] - 2 / 3) < 0.01, V and V["my_cover"]
    third = [r for r in V["plan"] if r["mechanic"].endswith("№3")][0]
    assert third["cd"] == "Божественный гимн" and third["like_top"], third
    assert X["pull_trend"]["rows"][0]["name"] == "Ледяная волна", X["pull_trend"]
    # Вайп: план покрывает и пики, до которых рейд не дошёл
    Rw = run_raid(FakeRaidClient(), DEMO_URL, 1, log=lambda m: None)
    assert any(r["my"] is None for r in Rw["extras"]["vs_top"]["rows"]), Rw["extras"]["vs_top"]["rows"]
    print("OK анализ: смерть нормирована, шум топа отсечён, дебаффы босса и выжимка на месте")


if __name__ == "__main__":
    test_analysis_quality()


def test_raid_rotation():
    """Ротация всего рейда: все DPS разобраны, общие проблемы найдены, Excel собирается."""
    import json
    import tempfile
    from wcl_analyzer import web
    from wcl_analyzer.raid_rotation import run_demo
    R = run_demo(log=lambda m: None)
    ok = [r for r in R["rows"] if "error" not in r]
    assert len(ok) == 5, R["rows"]
    worst = min(ok, key=lambda r: r["gap"])
    assert worst["name"] == "Ильвен" and worst["actions"][0]["title"].startswith("Смерть"), worst["actions"]
    best = max(ok, key=lambda r: r["gap"])
    assert best["name"] == "Кассия", best["name"]
    assert any("у 3 из 5" in line for line in R["brief"]), R["brief"]
    job = {"id": "t", "progress": 0.0, "log": []}
    web._run_raid_rotation(job, {"mode": "raidrot", "demo": True}, None, lambda m: None)
    assert len(json.dumps(job["result"])) < 2_000_000 and job["xlsx"]
    from openpyxl import load_workbook
    with tempfile.NamedTemporaryFile(suffix=".xlsx") as f:
        f.write(job["xlsx"]); f.flush()
        wb = load_workbook(f.name)
        assert wb.sheetnames[:2] == ["Ротация рейда", "Что исправить"] and "Ильвен" in wb.sheetnames, wb.sheetnames
    print("OK ротация рейда: 5 DPS, общие проблемы, переход к игроку, Excel")


if __name__ == "__main__":
    test_raid_rotation()


def test_batched_fetch():
    """Пакетный запрос (все выборки игрока одним GraphQL) даёт те же данные, что и десять отдельных."""
    import re
    from wcl_analyzer.api import Cache, WCLClient
    from wcl_analyzer.logs import _fetch_raw_batched, fetch_raw

    fake = FakeClient()
    code = "TOPREPORT001"
    report = fake.report(code)
    fight = report["fights"][0]
    actor = report["masterData"]["actors"][0]

    class Batched(WCLClient):
        calls = 0

        def query(self, q, variables=None, use_cache=True, max_age_s=None):
            Batched.calls += 1
            rep = {}
            for alias, args in re.findall(r"(e\d+): events\(([^)]*)\)", q):
                kv = dict(re.findall(r"(\w+): ([^,]+)", args))
                data = fake.events(code, 1, float(kv["startTime"]), float(kv["endTime"]), kv["dataType"],
                                   source_id=int(kv["sourceID"]) if "sourceID" in kv else None,
                                   target_id=int(kv["targetID"]) if "targetID" in kv else None,
                                   hostility=kv.get("hostilityType"))
                rep[alias] = {"data": data, "nextPageTimestamp": None}
            for alias in re.findall(r"(t\d+): table\(", q):
                rep[alias] = {"data": fake.damage_table(code, 1, actor["id"])}
            return {"reportData": {"report": rep}}

    class FakeRes(FakeClient):
        def events(self, *a, **k):
            return [] if a[4] == "Resources" else super().events(*a, **k)

    fake = FakeRes()
    from wcl_analyzer.api import MemoryCache
    b = Batched("id", "secret", MemoryCache(), verbose=False)
    got = _fetch_raw_batched(b, report, fight, actor["id"], False)
    want = fetch_raw(fake, report, fight, actor["id"])
    for k in ("casts", "buffs", "debuffs", "dmg_taken", "deaths", "boss_casts", "combatant", "boss_debuffs"):
        assert len(got[k]) == len(want[k]), (k, len(got[k]), len(want[k]))
    assert got["dmg_table"] == want["dmg_table"] and Batched.calls == 1, Batched.calls
    print("OK пакетный запрос: те же данные одним запросом вместо", 10)


def test_limit_no_hang():
    """Исчерпан часовой лимит: поиск боя сразу объясняет, а не висит; запрос лимита не зацикливается."""
    import time as _t

    from wcl_analyzer.api import MemoryCache, WCLClient, WCLError

    class Resp:
        status_code = 429
        text = "Too many requests"

        def json(self):
            return {}

    class Sess:
        posts = 0

        def post(self, url, **k):
            Sess.posts += 1
            if "token" in url:
                r = Resp(); r.status_code = 200; r.json = lambda: {"access_token": "t", "expires_in": 3600}
                return r
            return Resp()

    c = WCLClient("id", "secret", MemoryCache(), verbose=False)
    c.session = Sess()
    c.max_wait_s = 0
    t0 = _t.time()
    try:
        c.query("query { x }")
        raise AssertionError("должна быть ошибка")
    except WCLError as e:
        assert "лимит" in str(e) and "мин" in str(e), e
    assert _t.time() - t0 < 2 and Sess.posts < 6, Sess.posts
    print("OK лимит: поиск боя сразу сообщает о лимите, без зависания")


def test_pick_by_percentile():
    """Отбор игроков для ротации рейда по процентилю и ilvl% из рейтингов WCL."""
    from wcl_analyzer.collect import inspect_report
    from wcl_analyzer.raid_demo import CODE, DEMO_URL, FakeRaidClient
    from wcl_analyzer.raid_rotation import parse_pick, pick_players
    c = FakeRaidClient()
    kill = next(f for f in c.report(CODE)["fights"] if f.get("kill"))
    wipe = next(f for f in c.report(CODE)["fights"] if not f.get("kill"))
    insp = inspect_report(c, DEMO_URL, kill["id"])
    dps = [p for p in insp["players"] if p["role"] == "DPS"]
    assert all(p["rank"] is not None and p["ilvl"] is not None for p in insp["players"])
    assert parse_pick("rank:50") == ("rank", 50.0) and parse_pick("x:50") is None and parse_pick("rank:100") is None
    keep, out = pick_players(dps, "rank:50", log=lambda m: None)
    assert keep and all(p["rank"] <= 50 for p in keep) and all(p["rank"] > 50 for p in out)
    assert len(keep) + len(out) == len(dps)
    k2, _ = pick_players(dps, "ilvl:75", log=lambda m: None)
    assert all(p["ilvl"] <= 75 for p in k2)
    all_, none_ = pick_players(dps, None)
    assert all_ == dps and not none_
    # у вайпа рейтингов нет — разбираются все
    wd = [p for p in inspect_report(c, DEMO_URL, wipe["id"])["players"] if p["role"] == "DPS"]
    msgs = []
    k3, _ = pick_players(wd, "rank:25", log=msgs.append)
    assert k3 == wd and "нет рейтингов" in msgs[0]
    try:
        pick_players([{**p, "rank": 80.0} for p in dps], "rank:50", log=lambda m: None)
        raise AssertionError("должна быть ошибка: никто не прошёл отбор")
    except LookupError as e:
        assert "порог выше" in str(e)
    # вкладка «Рейдовые кулдауны в пики урона» в разборе игрока
    from wcl_analyzer import web
    job = {"id": "t", "progress": 0.0, "log": []}
    web._run_player(job, {"demo": True}, None, lambda m: None)
    import json
    assert "raid_peaks" not in job["result"], "полученный урон и сейвы — вкладка разбора рейда, не разбора игрока"
    # сравнение талантов с топом
    T = job["result"]["talents"]
    kinds = {r["kind"] for r in T["rows"]}
    assert {"missing", "choice"} <= kinds, kinds
    branches = {r["branch"] for r in T["rows"]}
    assert {"Класс", "Специализация", "Героическая: Вестник льда"} <= branches, branches
    assert T["hero"]["my"] == "Вестник льда" and T["hero_same_n"] >= 3
    # таланты, у которых в событии нет номера узла, находятся по справочнику
    from wcl_analyzer.talents import _picks, demo_tree_data, spec_tree
    nodes = spec_tree(demo_tree_data(), "Mage", "Frost")["nodes"]
    by_entry = {e: nid for nid, nd in nodes.items() for e in nd["entries"]}
    class _L:
        talent_tree = [(0, 1003, 1), (5004, 1004, 1)]
    assert _picks(_L(), by_entry) == {5003: (1003, 1), 5004: (1004, 1)}
    assert T["has_names"] and all(not r["name"].startswith("узел") for r in T["rows"])
    json.dumps(T)
    import openpyxl, io
    wb = openpyxl.load_workbook(io.BytesIO(job["xlsx"]))
    if wb is not None:
        assert "Таланты" in wb.sheetnames
    print(f"OK отбор по процентилю: ≤50% — {len(keep)} из {len(dps)} DPS, вайп — все")


def test_real_talent_data():
    """Если есть интернет (на GitHub — есть): настоящий справочник Raidbots читается правильно."""
    from wcl_analyzer import talents
    talents._MEM.clear()
    data = talents.load_tree_data(log=lambda m: None, save=False)
    talents._MEM.clear()
    if not data:
        print("ПРОПУЩЕН справочник талантов: нет доступа к Raidbots")
        print("::notice title=Справочник талантов::пропущен — нет доступа к Raidbots")
        return
    # каждый спек справочника разбирается без ошибок, а у лекарей находятся их рейдовые кулдауны
    from wcl_analyzer.raid_cds import candidates
    n_specs, found_cd = 0, 0
    for spec_data in data:
        cls, spec = spec_data.get("className"), spec_data.get("specName")
        t = talents.spec_tree(data, cls, spec)
        assert t and len(t["nodes"]) > 30, (cls, spec)
        n_specs += 1
        spells = {e[1] for n in t["nodes"].values() for e in n["entries"].values() if e[1]}
        for sid, name, _cd, core in candidates(cls, spec):
            if sid in spells:
                found_cd += 1
    for cls, spec in (("Mage", "Frost"), ("Priest", "Holy"), ("Evoker", "Preservation")):
        t = talents.spec_tree(data, cls, spec)
        assert any(n["choice"] for n in t["nodes"].values()), "нет узлов выбора"
        assert any(n["part"] == "hero" and n["sub"] for n in t["nodes"].values()), "нет героических узлов"
        assert t["subs"], "нет названий героических веток"
    print(f"::notice title=Рейдовые кулдауны::в справочнике талантов найдено {found_cd} кулдаунов из таблицы сейвов, спеков {n_specs}")
    print(f"OK справочник талантов Raidbots: {len(data)} спеков, героические ветки, узлы выбора")
    print(f"::notice title=Справочник талантов::прочитан, {len(data)} спеков")


def test_same_difficulty():
    """Эталон собирается только из боёв той же сложности: бой другой сложности в рейтинге пропускается."""
    import copy
    from wcl_analyzer.collect import collect_reference
    c = FakeClient()
    my = c.reports["MYREPORT0001"]["fights"][0]
    diff = int(my.get("difficulty") or 5)
    # лучший лог рейтинга подменяем на бой другой сложности
    best = c.ranks[0]["report"]["code"]
    c.reports[best] = copy.deepcopy(c.reports[best])
    for f in c.reports[best]["fights"]:
        f["difficulty"] = 4 if diff != 4 else 5
    msgs = []
    logs, _ = collect_reference(c, int(my["encounterID"]), "Mage", "Frost", diff, top_n=5, log=msgs.append, save=False)
    assert all(lg.report_code != best for lg in logs), "бой другой сложности попал в эталон"
    assert any("другая сложность" in m for m in msgs), msgs
    assert len(logs) == 5 and all(lg.difficulty == diff for lg in logs)
    try:
        collect_reference(c, int(my["encounterID"]), "Mage", "Frost", 0, top_n=5, log=lambda m: None, save=False)
        raise AssertionError("без сложности эталон собираться не должен")
    except LookupError as e:
        assert "сложность" in str(e)
    print("OK сложность: эталон только из боёв той же сложности, бой другой сложности пропущен")


def test_two_references():
    """Героический бой: эталон 1 — героический топ, эталон 2 — эпохальный; у каждого свой Excel."""
    import copy
    from wcl_analyzer import web

    class TwoDiff(FakeClient):
        def __init__(self):
            super().__init__()
            self.heroic = []
            for rk in self.ranks:
                code = rk["report"]["code"]
                hcode = "H" + code[1:]
                rep = copy.deepcopy(self.reports[code])
                rep["code"] = hcode
                for f in rep["fights"]:
                    f["difficulty"] = 4
                self.reports[hcode], self.raws[hcode] = rep, self.raws[code]
                self.heroic.append({**rk, "report": {**rk["report"], "code": hcode}})
            for f in self.reports["MYREPORT0001"]["fights"]:
                f["difficulty"] = 4

        def rankings(self, encounter_id, class_name, spec_name, difficulty, page=1, force=False, max_age_s=None):
            out = super().rankings(encounter_id, class_name, spec_name, difficulty, page, force, max_age_s)
            if difficulty == 4:
                out = {**out, "characterRankings": {"rankings": self.heroic, "hasMorePages": False}}
            return out

    c = TwoDiff()
    old = web.CLIENT_FACTORY
    web.CLIENT_FACTORY = lambda creds: c
    try:
        job = {"id": "two", "progress": 0.0, "log": []}
        web._run_player(job, {"url": "https://www.warcraftlogs.com/reports/MYREPORT0001#fight=1&source=7",
                              "fight": "1", "actor": "7", "ref": "top10"}, ("a", "b"), lambda m: None)
    finally:
        web.CLIENT_FACTORY = old
    R = job["result"]
    assert R["info"]["ref_difficulty"] == "героический" and R["info"]["ref_same_diff"] == R["info"]["ref_n"]
    A = R.get("alt")
    assert A and A["info"]["ref_difficulty"] == "эпохальный" and A["info"]["ref_same_diff"] == 0, "нет эпохального эталона"
    assert A["excel"].endswith("/alt") and job.get("xlsx") and job.get("xlsx_alt")
    crit = {x["criterion"]: x for x in A["reliability"]["criteria"]}
    assert crit["Босс, сложность, спек"]["level"] == "MEDIUM", crit["Босс, сложность, спек"]
    # без галочки — только героический
    web.CLIENT_FACTORY = lambda creds: c
    try:
        job2 = {"id": "one", "progress": 0.0, "log": []}
        web._run_player(job2, {"url": "https://www.warcraftlogs.com/reports/MYREPORT0001#fight=1&source=7",
                               "fight": "1", "actor": "7", "ref": "top10", "mythic": False}, ("a", "b"), lambda m: None)
    finally:
        web.CLIENT_FACTORY = old
    assert "alt" not in job2["result"]
    print("OK два эталона: героический топ и эпохальный топ, у каждого свой Excel")


def test_talent_data_without_ids():
    """Справочник талантов, где у части узлов и талантов нет номера (как в реальном Raidbots), — без ошибки 'id'."""
    from wcl_analyzer import talents
    from wcl_analyzer.raid_cds import roster_cds
    data = [{"className": "Priest", "specName": "Holy",
             "classNodes": [{"name": "без номера", "entries": [{"name": "x"}]}],
             "specNodes": [{"id": 1, "name": "Божественный гимн", "type": "single",
                            "entries": [{"id": 11, "name": "Божественный гимн", "spellId": 64843}, {"name": "без номера"}]},
                           {"id": 2, "name": "Апофеоз", "type": "single", "entries": [{"id": 21, "spellId": 200183}]}],
             "heroNodes": [{"id": 3, "subTreeId": 7, "entries": [{"id": 31, "name": "Герой"}]}],
             "subTreeNodes": [{"id": 4, "entries": [{"traitSubTreeId": 7, "name": "Ветка"}, {"name": "без номера"}]}, {}]},
            "мусор"]
    t = talents.spec_tree(data, "Priest", "Holy")
    assert set(t["nodes"]) == {1, 2, 3} and t["subs"] == {7: "Ветка"}
    raw = {"details": {"healers": [{"id": 5, "name": "Элария", "type": "Priest", "specs": [{"spec": "Holy"}]}, {"name": "без id"}]},
           "combatant": [{"sourceID": 5, "talentTree": [{"id": 11, "nodeID": 1, "rank": 1}, {"nodeID": 99}]}],
           "report": {"masterData": {"abilities": [{"gameID": 64843, "name": "Божественный гимн"}, {"name": "без номера"}]}}}
    R = {"info": {"duration_s": 300}, "extras": {"raid_cds": []}}
    roster = roster_cds(raw, R, data)
    names = {c["name"] for c in roster}
    assert "Божественный гимн" in names and "Апофеоз" not in names, names  # гимн взят, апофеоз — нет
    # способность не из дерева талантов (базовая) с данными о талантах не выкидывается
    raw["details"]["dps"] = [{"id": 6, "name": "Торвин", "type": "Warrior", "specs": [{"spec": "Fury"}]}]
    raw["combatant"].append({"sourceID": 6, "talentTree": [{"id": 11, "nodeID": 1}]})
    data.append({"className": "Warrior", "specName": "Fury", "classNodes": [], "heroNodes": [], "subTreeNodes": [],
                 "specNodes": [{"id": 1, "entries": [{"id": 11, "spellId": 1}]}]})
    assert any(c["name"] == "Ободряющий клич" for c in roster_cds(raw, R, data)), "базовый кулдаун пропал"
    print("OK справочник талантов без номеров у части узлов: без ошибки 'id', таланты состава учтены")


def test_single_sources():
    """Одна таблица игровых данных и один словарь названий — без копий в разных местах."""
    import json as _json
    from wcl_analyzer import game_data, web
    from wcl_analyzer.names_ru import CLASSES
    d = _json.loads(game_data.BUNDLED.read_text(encoding="utf-8"))
    assert game_data._valid(d)
    seen = set()
    for c in d["raid_cds"]:
        assert c["class"] in CLASSES and c["cd"] > 0 and 1 <= c["power"] <= 3 and c["name"] and c["en"], c
        key = (c["class"], c.get("spec"), c["id"])
        assert key not in seen, key
        seen.add(key)
        if c.get("choice_with"):
            assert any(x["id"] == c["choice_with"] for x in d["raid_cds"]), c
    assert 64843 in game_data.cd_ids() and game_data.cooldown(64843) == 180 and 2825 in game_data.lust_ids()
    assert game_data.cd_name_re().search("Healing Tide Totem") and not game_data.cd_name_re().search("Shadow Word: Pain")
    page = web._page().decode("utf-8")
    assert "/*CLASS_RU*/" not in page and "рыцарь смерти" in page and '"Mage|Frost": "Лёд"' in page
    print("OK один источник: таблица игровых данных и словарь названий")


def test_code_update():
    """Обновление без переустановки: скачанный код подключается при запуске; сломанный — откат на встроенный."""
    import hashlib, io, json as _json, subprocess, tempfile, zipfile
    from pathlib import Path as _P
    from wcl_analyzer import update
    root = _P(__file__).resolve().parents[1]
    tmp = _P(tempfile.mkdtemp())
    shell = "testshell01"
    # архив кода «сборки 77» — как его собирает GitHub
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for f in (root / "wcl_analyzer").rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts:
                rel = f.relative_to(root).as_posix()
                data = f.read_bytes()
                if rel == "wcl_analyzer/_build.py":
                    data = f'BUILD = "77"\nSHELL_ID = "{shell}"\n'.encode()
                z.writestr(rel, data)
    code = buf.getvalue()
    info = {"build": 77, "shell": shell, "sha256": hashlib.sha256(code).hexdigest()}
    old = (update._get, update.shell_id, update.code_root)
    update._get = lambda url, timeout=30: _json.dumps(info).encode() if url.endswith("update.json") else code
    update.shell_id = lambda: shell
    update.code_root = lambda: tmp / "code"
    try:
        res = update.apply_code()
        assert res["ok"] and res["build"] == 77 and (tmp / "code" / "current.json").exists()
        # испорченный архив не принимается
        update._get = lambda url, timeout=30: (_json.dumps({**info, "sha256": "0" * 64}).encode()
                                               if url.endswith("update.json") else code)
        try:
            update.apply_code()
            raise AssertionError("испорченный архив принят")
        except ValueError as e:
            assert "контрольная сумма" in str(e)
        # GitHub недоступен — номер сборки и код берутся через jsDelivr (по файлам)
        import requests as _rq
        files = {"/" + f.relative_to(root).as_posix(): f.read_bytes() for f in (root / "wcl_analyzer").rglob("*")
                 if f.is_file() and "__pycache__" not in f.parts}
        shell_j = update.shell_fingerprint([(root / f).read_bytes() for f in update.SHELL_FILES])

        def fake_get(url, timeout=30):
            if "github.com" in url:
                raise _rq.ConnectionError("blocked")
            if url.endswith("/resolved?specifier=latest"):
                return b'{"version": "1.1.78"}'
            if "structure=flat" in url:
                return _json.dumps({"files": [{"name": n} for n in files]}).encode()
            path = url.split("@1.1.78", 1)[1]
            return files[path] if path in files else (root / path.lstrip("/")).read_bytes()
        update._get, update.shell_id = fake_get, (lambda: shell_j)
        chk = update.check()
        assert chk["source"] == "jsdelivr" and chk["latest"] == 78 and chk["kind"] == "code", chk
        res = update.apply_code()
        assert res["build"] == 78
        built = (tmp / "code" / "build-78" / "wcl_analyzer" / "_build.py").read_text()
        assert 'BUILD = "78"' in built and shell_j in built
        # всё недоступно — понятная причина, а не «нет связи с Warcraft Logs»
        update._get = lambda url, timeout=30: (_ for _ in ()).throw(_rq.ConnectionError("down"))
        try:
            update.check()
            raise AssertionError("ошибка не выдана")
        except update.UpdateError as e:
            assert "github.com — нет соединения" in str(e) and "jsdelivr.net" in str(e), e
        # вернуть «сборку 77» для проверок загрузчика ниже
        update._get = lambda url, timeout=30: _json.dumps(info).encode() if url.endswith("update.json") else code
        update.shell_id = lambda: shell
        update.apply_code()
    finally:
        update._get, update.shell_id, update.code_root = old
    probe = (f"import sys; sys.path.insert(0, {str(root)!r}); import wcl_boot; wcl_boot.SHELL_ID = {shell!r}; "
             "b = wcl_boot.activate(log=lambda m: print('LOG', m)); import wcl_analyzer, wcl_analyzer._build as v; "
             "print(b, v.BUILD, 'code' in wcl_analyzer.__file__)")
    env = {**__import__("os").environ, "WCL_DATA_DIR": str(tmp)}
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env, cwd=str(tmp)).stdout.split()
    assert out[-3:] == ["77", "77", "True"], out
    # другая оболочка — скачанный код не берётся
    out = subprocess.run([sys.executable, "-c", probe.replace(repr(shell), "'other'", 1)], capture_output=True, text=True,
                         env=env, cwd=str(tmp)).stdout.split()
    assert out[-3:] == ["None", "dev", "False"], out
    # сломанный код — откат на встроенный, обновление помечено сломанным
    cur = _json.loads((tmp / "code" / "current.json").read_text())
    (_P(cur["path"]) / "wcl_analyzer" / "web.py").write_text("def broken(:\n")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env, cwd=str(tmp)).stdout
    assert out.split()[-3:] == ["None", "dev", "False"] and "не запустилось" in out, out
    assert (tmp / "code" / "failed.json").exists()
    print("OK обновление кода: подключается при запуске, чужая оболочка и сломанный код — встроенная версия")


def test_android_java_threads():
    """Android: классы приложения из потока сервера не находятся — загружаем их заранее в главном потоке."""
    import threading
    import types
    calls = []

    class _Cls:
        def __init__(self, name):
            self.name = name
            self.mActivity = self
            self.SDK_INT = 33
            self.ACTION_VIEW, self.FLAG_ACTIVITY_NEW_TASK = "view", 1

        def __call__(self, *a):
            return self

        def parse(self, u):
            return u

        def addFlags(self, f):
            pass

        def startActivity(self, intent):
            calls.append("open")

    def autoclass(name):
        if name.startswith("org.kivy") and threading.current_thread() is not threading.main_thread():
            raise Exception("JVM exception occurred: java.lang.ClassNotFoundException")
        return _Cls(name)

    sys.modules["jnius"] = types.SimpleNamespace(autoclass=autoclass)
    try:
        from wcl_analyzer import platform_support as ps
        ps._J.clear()
        errs = []
        # без загрузки заранее — в потоке ошибка (как была у пользователя)
        def no_preload():
            try:
                ps.android_open_url("https://x")
            except Exception as e:  # noqa: BLE001
                errs.append(str(e))
        t = threading.Thread(target=no_preload); t.start(); t.join()
        assert errs and "PythonActivity" in errs[0], errs
        ps._J.clear()
        ps.android_preload()  # главный поток — как в serve()
        t = threading.Thread(target=lambda: ps.android_open_url("https://x")); t.start(); t.join()
        assert calls == ["open"], calls
    finally:
        del sys.modules["jnius"]
        ps._J.clear()
    print("OK Android: классы Java загружаются в главном потоке — сохранение файлов и ссылки работают из потоков")


if __name__ == "__main__":
    test_batched_fetch()
    test_android_java_threads()
    test_code_update()
    test_single_sources()
    test_talent_data_without_ids()
    test_two_references()
    test_same_difficulty()
    test_real_talent_data()
    test_pick_by_percentile()
    test_limit_no_hang()

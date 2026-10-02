"""Сравнение талантов с топом: какие узлы дерева у топа почти у всех, а у вас нет; что взято у вас,
но редко у топа; где в узле выбора вы взяли другой талант; какая героическая ветка у топа.

В логе Warcraft Logs таланты — только номера узлов и выбранных талантов. Названия берутся из
открытого справочника Raidbots (talents.json): он скачивается один раз и хранится на этом
устройстве неделю. Если справочник недоступен, вместо названий будут номера узлов.
"""
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

TALENTS_URL = "https://www.raidbots.com/static/data/live/talents.json"
MAX_AGE_S = 7 * 86400
COMMON = 0.6     # «почти у всех топа»: узел взят у 60%+ игроков топа
RARE = 0.3       # «редко у топа»: узел взят меньше чем у 30%
MIN_GROUP = 3    # для сравнения DPS «с талантом / без» нужно хотя бы 3 игрока в каждой группе

_MEM: dict = {}


def _file() -> Path:
    from .config import data_dir
    return data_dir() / "talents.json"


def load_tree_data(log=print, save: bool = True) -> list | None:
    """Справочник талантов Raidbots: из памяти, с диска (не старше недели) или из интернета.
    save=False — публичный сервер: ничего не пишет на диск, справочник живёт только в памяти."""
    if "data" in _MEM:
        return _MEM["data"]
    f = _file()
    data = None
    try:
        if f.exists() and time.time() - f.stat().st_mtime < MAX_AGE_S:
            data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if data is None:
        try:
            import requests
            r = requests.get(TALENTS_URL, timeout=30)
            r.raise_for_status()
            data = r.json()
            if save:
                try:
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(json.dumps(data), encoding="utf-8")
                except OSError:
                    pass
        except Exception as e:  # noqa: BLE001
            log(f"Справочник талантов недоступен ({e}): вместо названий будут номера узлов")
            try:  # хоть устаревший
                data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
            except (OSError, ValueError):
                data = None
    _MEM["data"] = data
    return data


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def spec_tree(data: list | None, cls: str, spec: str) -> dict | None:
    """Узлы дерева спека: {узел: {"name", "kind", "entries": {талант: (имя, spellId)}, "max", "hero", "sub"}}."""
    if not data:
        return None
    c, s = _norm(cls), _norm(spec)
    t = next((x for x in data if isinstance(x, dict) and _norm(x.get("className")) == c
              and _norm(x.get("specName")) == s), None)
    if not t:
        return None
    nodes: dict = {}
    for part, kind in (("classNodes", "class"), ("specNodes", "spec"), ("heroNodes", "hero")):
        for n in t.get(part) or []:
            if not isinstance(n, dict) or n.get("id") is None:
                continue  # в справочнике бывают узлы без номера — пропускаем, а не падаем
            nodes[int(n["id"])] = {
                "name": n.get("name") or "", "part": kind, "choice": n.get("type") == "choice",
                "max": int(n.get("maxRanks") or 1), "sub": n.get("subTreeId"),
                "entries": {int(e["id"]): (e.get("name") or "", e.get("spellId"))
                            for e in n.get("entries") or [] if isinstance(e, dict) and e.get("id") is not None}}
    subs = {}
    for n in t.get("subTreeNodes") or []:
        for e in (n.get("entries") or []) if isinstance(n, dict) else []:
            if e.get("traitSubTreeId") is not None:
                subs[int(e["traitSubTreeId"])] = e.get("name") or ""
    return {"nodes": nodes, "subs": subs}


def _picks(log) -> dict[int, tuple[int, int]]:
    return {n: (e, r) for n, e, r in (log.talent_tree or [])}


def compare_talents(me, tops: list, data: list | None = None, log=print) -> dict | None:
    """Сравнение талантов игрока с игроками топа (все скачанные логи, без отбора по билду)."""
    tops = [t for t in tops if t.talent_tree]
    if not me.talent_tree or len(tops) < 3:
        return None
    if data is None:
        data = load_tree_data(log)
    tree = spec_tree(data, me.cls, me.spec)
    nodes = (tree or {}).get("nodes", {})
    names = dict(me.names or {})

    def entry_name(node: int, entry: int) -> str:
        nd = nodes.get(node)
        if nd and entry in nd["entries"]:
            en, sid = nd["entries"][entry]
            return names.get(int(sid), en) if sid else en  # русское название из отчёта, если есть
        return nd["name"] if nd else f"узел {node}"

    def node_name(node: int) -> str:
        nd = nodes.get(node)
        if not nd:
            return f"узел {node}"
        if len(nd["entries"]) == 1:
            return entry_name(node, next(iter(nd["entries"])))
        return " / ".join(entry_name(node, e) for e in nd["entries"])

    mine = _picks(me)
    tp = [_picks(t) for t in tops]
    n = len(tops)
    dps = [t.dps for t in tops]
    taken = Counter(nd for p in tp for nd in p)
    rows = []

    def dps_split(has):
        a = [d for d, h in zip(dps, has) if h]
        b = [d for d, h in zip(dps, has) if not h]
        return (median(a) if len(a) >= MIN_GROUP else None, median(b) if len(b) >= MIN_GROUP else None, len(a), len(b))

    for node in sorted(set(taken) | set(mine)):
        if nodes.get(node, {}).get("part") == "hero":
            continue  # героическая ветка сравнивается отдельно
        share = taken[node] / n
        if node in mine:
            entry, rank = mine[node]
            if share <= RARE:
                w, wo, nw, nwo = dps_split([node in p for p in tp])
                rows.append({"kind": "extra", "name": entry_name(node, entry), "my": "взят", "top": "редко",
                             "share": share, "dps_with": w, "dps_without": wo, "n_with": nw, "n_without": nwo})
                continue
            # узел выбора: у вас другой талант, чем у большинства взявших
            ch = Counter(p[node][0] for p in tp if node in p)
            best, cnt = ch.most_common(1)[0]
            if best != entry and cnt / max(1, taken[node]) >= COMMON:
                w, wo, nw, nwo = dps_split([p.get(node, (None,))[0] == best for p in tp])
                rows.append({"kind": "choice", "name": entry_name(node, best), "my": entry_name(node, entry),
                             "top": entry_name(node, best), "share": cnt / n,
                             "dps_with": w, "dps_without": wo, "n_with": nw, "n_without": nwo})
                continue
            ranks = [p[node][1] for p in tp if node in p]
            if ranks and rank < median(ranks) and share >= COMMON:
                rows.append({"kind": "rank", "name": entry_name(node, entry), "my": f"ранг {rank}",
                             "top": f"ранг {median(ranks):g}", "share": share,
                             "dps_with": None, "dps_without": None, "n_with": None, "n_without": None})
        elif share >= COMMON:
            ch = Counter(p[node][0] for p in tp if node in p)
            best = ch.most_common(1)[0][0]
            w, wo, nw, nwo = dps_split([node in p for p in tp])
            rows.append({"kind": "missing", "name": entry_name(node, best), "my": "нет", "top": "взят",
                         "share": share, "dps_with": w, "dps_without": wo, "n_with": nw, "n_without": nwo})
    order = {"missing": 0, "choice": 1, "extra": 2, "rank": 3}
    rows.sort(key=lambda r: (order[r["kind"]], -abs(r["share"] - 0.5)))

    # Героическая ветка: по узлам героического дерева, которые взяты
    def hero_of(p):
        cnt = Counter(nodes[nd]["sub"] for nd in p if nodes.get(nd, {}).get("part") == "hero" and nodes[nd]["sub"])
        return cnt.most_common(1)[0][0] if cnt else None
    subs = (tree or {}).get("subs", {})
    my_hero = hero_of(mine)
    top_heroes = Counter(h for h in (hero_of(p) for p in tp) if h is not None)
    hero = None
    if top_heroes:
        hero = {"my": subs.get(my_hero, None) if my_hero else None,
                "top": [{"name": subs.get(h, f"ветка {h}"), "share": c / n,
                         "dps": median([d for d, p in zip(dps, tp) if hero_of(p) == h])} for h, c in top_heroes.most_common()]}

    sims = [len(set(mine) & set(p)) / max(1, len(set(mine) | set(p))) for p in tp]
    closest = max(sims) if sims else 0.0
    same = sum(1 for x in sims if x >= 0.85)
    summary = []
    if hero and hero["my"] and hero["top"] and hero["top"][0]["name"] != hero["my"] and hero["top"][0]["share"] >= COMMON:
        summary.append(f"Героическая ветка: у вас «{hero['my']}», у топа — «{hero['top'][0]['name']}» в {hero['top'][0]['share']:.0%} логов")
    miss = [r for r in rows if r["kind"] == "missing"]
    chg = [r for r in rows if r["kind"] == "choice"]
    if miss or chg:
        parts = []
        if miss:
            parts.append(f"не взято популярных у топа — {len(miss)} ({', '.join(r['name'] for r in miss[:3])}{'…' if len(miss) > 3 else ''})")
        if chg:
            parts.append(f"другой выбор в узлах выбора — {len(chg)}")
        summary.append("Таланты: " + "; ".join(parts))
    elif rows:
        summary.append("Таланты почти как у большинства топа: отличия только в редких узлах")
    else:
        summary.append("Таланты как у большинства топа")
    return {"n": n, "rows": rows, "hero": hero, "same_build": same, "closest": closest,
            "has_names": bool(tree), "summary": summary}


def _w(k: int, one: str, few: str, many: str) -> str:
    k = abs(k) % 100
    if 10 < k < 20:
        return many
    k %= 10
    return one if k == 1 else few if 1 < k < 5 else many


def demo_tree_data() -> list:
    """Справочник для демо: узлы 5000–5047 демо-логов с понятными названиями."""
    names = ["Ледяные копья", "Ледяной покров", "Замораживание", "Холодная кровь", "Ледяная буря",
             "Пронизывающий холод", "Осколки", "Мороз", "Ледяная корона", "Блеск льда"]
    spec_nodes = []
    for k in range(48):
        nid = 5000 + k
        if k in (10, 11):  # узлы выбора
            spec_nodes.append({"id": nid, "name": "Выбор", "type": "choice", "maxRanks": 1,
                               "entries": [{"id": 1000 + k, "name": f"{names[k % 10]} (вариант А)", "spellId": None},
                                           {"id": 9000 + k, "name": f"{names[k % 10]} (вариант Б)", "spellId": None}]})
        else:
            spec_nodes.append({"id": nid, "name": f"{names[k % 10]} {k // 10 + 1}", "type": "single", "maxRanks": 1,
                               "entries": [{"id": 1000 + k, "name": f"{names[k % 10]} {k // 10 + 1}", "spellId": None}]})
    return [{"className": "Mage", "specName": "Frost", "classNodes": [], "specNodes": spec_nodes,
             "heroNodes": [], "subTreeNodes": []}]

"""Практический гайд эталона: общие данные для Excel и веб-интерфейса."""
from __future__ import annotations

from .compare import _c, _fmt_t, _sec


def _mln(x: float) -> str:
    if x >= 1e6:
        return f"{x / 1e6:.1f}".replace(".", ",") + " млн"
    return f"{x / 1e3:.0f} тыс." if x >= 1e3 else f"{x:.0f}"
from .reference import Reference


def guide_sections(ref: Reference) -> list[dict]:
    """[{title, rows: [[когда, что делать, подтверждение, доверие|None]]}]"""
    agg = ref.agg
    out: list[dict] = []

    def add(title, rows):
        out.append({"title": title, "rows": rows})

    pot = next((sp.name for sp in ref.spells.values() if sp.category == "potion"), "Зелье")
    pre = []
    if agg["prepot_share"] >= 0.5:
        pre.append(["Перед пуллом", f"{pot} (пре-пот)", f"{agg['prepot_share']:.0%} игроков", None])
    add("Перед боем", pre)
    add("Опенер", [[_fmt_t(o["t"]), ref.name_of(o["id"]), f"{o['share']:.0%} игроков", o["confidence"]]
                   for o in agg["opener"]])

    rot = [(ab, a) for ab, a in agg["abilities"].items()
           if ref.spells[ab].category == "rotation" and a["share"] >= 0.5]
    rot.sort(key=lambda x: -(x[1]["cpm"]["median"] or 0))
    rows = [["Постоянно", f"{ref.name_of(ab)}: ≈ {_c(a['cpm']['median'])} в минуту", f"{a['share']:.0%} игроков", None]
            for ab, a in rot]
    for p in ref.procs.values():
        a = agg["procs"][p.id]
        rows.append(["При проке", f"{p.name} → {p.consumer_name} (реакция ≈ {_c(a['reaction']['median'] or 0)} с)",
                     f"использовано {a['used']['median']:.0f} из {a['gained']['median']:.0f}", None])
    for g in agg["ngrams"][4][:3]:
        rows.append(["Связка", " → ".join(ref.name_of(x) for x in g["seq"]),
                     f"{g['player_share']:.0%} игроков", None])
    add("Ротация: что нажимать", rows)

    main = ref.name_of(ref.main_cd) if ref.main_cd else "главный кулдаун"
    rows = []
    for w in agg["burst"]:
        comps = ", ".join(f"{ref.name_of(c['id'])} ({_sec(c['offset'])})" for c in w["components"])
        rows.append([_fmt_t(w["t0"]["median"]), main + (f" + {comps}" if comps else ""),
                     f"{w['players']}/{ref.n} игроков", None])
    add("Бурст", rows)

    rows = []
    for ab, d in agg["defensives"].items():
        if d["mechanic"] is None or d["users"] < 0.3 * ref.n:
            continue
        rows.append([f"за {_c(d['offset'])} с до удара", f"{ref.name_of(ab)} перед ударом «{ref.name_of(d['mechanic'])}»",
                     f"{d['link_share']:.0%} применений", d["confidence"]])
    add("Защитные способности", rows)
    add("Зелья", [[_fmt_t(p["median"]), pot, f"{p['share']:.0%} игроков", None] for p in agg["potions"]])
    if agg["lust"]["median"] is not None:
        add("Жажда крови", [[_fmt_t(agg["lust"]["median"]), "Жажда крови / Героизм в рейде",
                           f"{agg['lust_share']:.0%} боёв", None]])
    rows = []
    for ab, m in agg["mechanics"].items():
        if m["is_key"]:
            times = ", ".join(_fmt_t(o["median"]) for o in m["occurrences"][:6])
            rows.append([times, f"«{ref.name_of(ab)}»", f"урон ≈ {_mln(m['damage'])} за бой", None])
    add("Ключевые механики", rows)
    return out

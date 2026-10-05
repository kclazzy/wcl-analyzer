"""Прогон настоящего лога через разбор боя (все боссы): сводка для проверки, без ключа в выводе."""
import json, os, sys, time, traceback
sys.path.insert(0, ".")
from wcl_analyzer.api import Cache, WCLClient
from wcl_analyzer.raid import run_raid
from wcl_analyzer.raid_saves import pick_fights
from wcl_analyzer.raid_top import mrt_note
from wcl_analyzer.logs import parse_report_url

url = sys.argv[1]
cl = WCLClient(os.environ["WCL_CLIENT_ID"], os.environ["WCL_CLIENT_SECRET"], Cache("dbg_cache.sqlite"), verbose=False)
code, _, _ = parse_report_url(url)
rep = cl.report(code)
out = {"report": {"title": rep.get("title"), "zone": (rep.get("zone") or {}).get("name"),
                  "fights": [(f["id"], f["name"], f.get("difficulty"), f.get("kill"), round((f["endTime"] - f["startTime"]) / 1000))
                             for f in rep["fights"]]}, "bosses": []}
log = []
for c in pick_fights(rep, difficulties=None):
    f = c["fight"]
    t0, r0 = time.time(), cl.requests_made
    item = {"fight": f["id"], "boss": f["name"], "difficulty": f.get("difficulty"), "kill": f.get("kill")}
    try:
        R = run_raid(cl, url, int(f["id"]), log=lambda m: log.append(m), talent_data=[], save_talents=False, mythic=False)
        X = R["extras"]
        B, T = X.get("burst") or {}, X.get("tank") or {}
        used = {(p["player"], p["cd"]) for r in X.get("plan") or [] for p in r["picks"] + (r.get("heal_picks") or [])}
        item.update({
            "secs": round(time.time() - t0, 1), "requests": cl.requests_made - r0,
            "info": {k: R["info"].get(k) for k in ("duration", "difficulty", "size", "tanks", "healers", "dps", "phases")},
            "brief": R["brief"], "spikes": [(s["t"], s["ability"], s["damage"], s.get("deaths")) for s in X.get("spikes") or []],
            "roster_cds": [(c_["player"], c_["name"], c_["cd"], c_.get("used")) for c_ in X.get("roster_cds") or []],
            "idle_cds": [(c_["player"], c_["name"]) for c_ in X.get("roster_cds") or [] if (c_["player"], c_["name"]) not in used],
            "plan": [{"time": r["time"], "mech": r["mechanic"], "need": r["need"], "picks": [(p["player"], p["cd"], p.get("extra")) for p in r["picks"]],
                      "heal": [(p["player"], p["cd"]) for p in r.get("heal_picks") or []], "after_end": r.get("after_end")} for r in X.get("plan") or []],
            "mrt": mrt_note(X.get("plan") or [], f["name"]),
            "raid_cds": [(c_["player"], c_["name"], c_["time"]) for c_ in X.get("raid_cds") or []],
            "burst": {"hints": B.get("hints"), "lusts": B.get("lusts"), "windows": [(w["name"], w["time"], w.get("amp"), len(w.get("hit") or []), w.get("missed_ready")) for w in B.get("windows") or []],
                      "bursts": [(r["player"], r["spec"], [(c_["name"], c_["used"], c_["max_uses"], c_["proc"], c_["lazy"]) for c_ in r["cds"]], r["with_lust"]) for r in B.get("bursts") or []],
                      "error": B.get("error")},
            "tank": {"hints": T.get("hints"), "busters": T.get("busters"), "error": T.get("error"),
                     "events": [(e["time"], e["ability"], e["tank"], e["damage"], e.get("mitigated"), [x["name"] for x in e["own"]], [x["name"] for x in e["ext"]], e["ready"], e.get("held"), e["bare"], e["died"]) for e in T.get("events") or []],
                     "cds": [(c_["tank"], c_["name"], c_["time"], c_["used"], c_["max_uses"]) for c_ in T.get("cds") or []],
                     "mrt": T.get("mrt"), "cand": sorted(list(__import__("wcl_analyzer.raid_tank", fromlist=["x"]).last_candidates), key=lambda c: ("base" not in c, -(c.get("total") or 0)))[:15]},
            "deaths": [(d["time"], d["player"], d["ability"], d.get("wipe_tail")) for d in R.get("deaths") or []],
            "top": {"n": (X.get("vs_top") or {}).get("n")},
        })
    except Exception as e:
        item["error"] = f"{type(e).__name__}: {e}"
        item["trace"] = traceback.format_exc()[-3000:]
    out["bosses"].append(item)
    print(item.get("boss"), item.get("secs"), item.get("requests"), item.get("error"), flush=True)
try:
    out["points_left"] = cl.points_left()
except Exception:
    pass
out["log_tail"] = log[-200:]
json.dump(out, open("debug_log_out.json", "w"), ensure_ascii=False, indent=1, default=str)

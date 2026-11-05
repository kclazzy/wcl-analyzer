"""WoW Forever вышел: рейды и боссы на forever.warcraftlogs.com, свежие отчёты с боссами — для разбора."""
import os, json, sys
sys.path.insert(0, ".")
from wcl_analyzer.api import Cache, WCLClient
cl = WCLClient(os.environ["WCL_CLIENT_ID"], os.environ["WCL_CLIENT_SECRET"], Cache("dbg_probe.sqlite"), verbose=False).for_site("forever")
out = {}
try:
    w = cl.query("{worldData{expansions{id name zones{id name frozen difficulties{id name sizes} encounters{id name}}}}}", {})
    out["expansions"] = w["worldData"]["expansions"]
except Exception as e:
    out["expansions"] = repr(e)
try:
    g = cl.query("{gameData{classes{id name slug specs{id name slug}}}}", {})
    out["classes"] = g["gameData"]["classes"]
except Exception as e:
    out["classes"] = repr(e)
reps = []
zones = [z for e in (out["expansions"] if isinstance(out["expansions"], list) else []) for z in e.get("zones") or []]
for z in zones:
    try:
        r = cl.query("query($z:Int){reportData{reports(zoneID:$z, limit:25){data{code title startTime fights(killType:Encounters){id encounterID name difficulty kill size}}}}}", {"z": z["id"]})
        for rep in r["reportData"]["reports"]["data"]:
            kills = [f for f in rep.get("fights") or [] if f.get("kill")]
            reps.append({"zone": z["name"], "code": rep["code"], "title": rep["title"], "fights": len(rep.get("fights") or []), "kills": [(f["name"], f["difficulty"], f.get("size")) for f in kills][:10]})
    except Exception as e:
        out.setdefault("rep_err", []).append(f"{z['name']}: {e!r}"[:300])
out["reports"] = sorted(reps, key=lambda r: -len(r["kills"]))[:30]
try:
    out["points_left"] = cl.points_left()
except Exception:
    pass
json.dump(out, open("debug_forever.json", "w"), ensure_ascii=False, indent=1)
best = out["reports"][0]["code"] if out["reports"] else ""
open("forever_code.txt", "w").write(best)

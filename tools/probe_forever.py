"""Где Warcraft Logs держит WoW Forever: дополнения и рейды на всех сайтах + новые зоны в свежих отчётах."""
import os, json, sys
sys.path.insert(0, ".")
from wcl_analyzer.api import Cache, WCLClient
base = WCLClient(os.environ["WCL_CLIENT_ID"], os.environ["WCL_CLIENT_SECRET"], Cache("dbg_probe.sqlite"), verbose=False)
out = {}
KEYS = ("forever", "hyjal", "barrow", "blackmaw", "shaper", "skyborne", "riverlands")
for site in ["forever", "classic", "fresh", "sod", "vanilla", "www", "era", "classicplus", "plus"]:
    cl = base.for_site(site) if site in ("forever", "classic", "fresh", "sod", "vanilla", "www") else None
    if cl is None:
        import copy
        cl = copy.copy(base); cl.site = site
    rec = {}
    try:
        w = cl.query("{worldData{expansions{id name zones{id name encounters{id name}}}}}", {})
        exps = w["worldData"]["expansions"]
        rec["expansions"] = [(e["id"], e["name"], len(e.get("zones") or [])) for e in exps]
        rec["hits"] = [(e["name"], z["id"], z["name"], [x["name"] for x in z.get("encounters") or []][:15])
                       for e in exps for z in e.get("zones") or []
                       if any(k in (z["name"] + e["name"] + " ".join(x["name"] for x in z.get("encounters") or [])).lower() for k in KEYS)]
        rec["max_zone"] = max(((z["id"], z["name"], e["name"]) for e in exps for z in e.get("zones") or []), default=None)
    except Exception as e:
        rec["err"] = repr(e)[:300]
    out[site] = rec
json.dump(out, open("debug_forever.json", "w"), ensure_ascii=False, indent=1)
for k, v in out.items():
    print(k, json.dumps(v, ensure_ascii=False)[:600])

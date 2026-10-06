import json, os, sys
sys.path.insert(0, ".")
from wcl_analyzer.api import Cache, WCLClient
cl = WCLClient(os.environ["WCL_CLIENT_ID"], os.environ["WCL_CLIENT_SECRET"], Cache("dbg_cache.sqlite"), verbose=False)
code, fid = "39knxwWmHgCj6Bzr", 30
rep = cl.report(code)
f = next(x for x in rep["fights"] if int(x["id"]) == fid)
s, e = float(f["startTime"]), float(f["endTime"])
out = {}
q = """query($code: String!, $fid: [Int]) { reportData { report(code: $code) {
  t: table(fightIDs: $fid, dataType: Healing)
  g: graph(fightIDs: $fid, dataType: Healing)
  d: playerDetails(fightIDs: $fid)
} } }"""
r = cl.query(q, {"code": code, "fid": [fid]})["reportData"]["report"]
t = r["t"]; t = t.get("data", t) if isinstance(t, dict) else t
out["table_keys"] = list(t.keys()) if isinstance(t, dict) else str(type(t))
ents = t.get("entries") if isinstance(t, dict) else None
out["table_entry0"] = {k: v for k, v in (ents or [{}])[0].items() if k not in ("abilities", "targets", "talents", "gear")} if ents else None
out["table_entry0_keys"] = list((ents or [{}])[0].keys())
g = r["g"]; g = g.get("data", g) if isinstance(g, dict) else g
out["graph_keys"] = list(g.keys()) if isinstance(g, dict) else str(type(g))
ser = g.get("series") if isinstance(g, dict) else None
out["graph_series_n"] = len(ser or [])
out["graph_series0"] = {k: (v[:10] if isinstance(v, list) else v) for k, v in (ser or [{}])[0].items()} if ser else None
out["graph_other"] = {k: (v if not isinstance(v, list) else v[:3]) for k, v in g.items() if k != "series"} if isinstance(g, dict) else None
det = cl.unwrap_details(r["d"])
heal_ids = [int(p["id"]) for p in det.get("healers") or []]
out["healers"] = [(p["name"], p.get("type"), p.get("specs")) for p in det.get("healers") or []]
ev = cl.events(code, fid, s, s + 60000, "Casts", source_id=heal_ids[0], include_resources=True)
out["cast_res"] = [x for x in ev if x.get("type") == "cast"][:3]
ev2 = cl.events(code, fid, s, s + 20000, "Casts", include_resources=True,
                filter_expression=f'type = "cast" and source.id in ({", ".join(map(str, heal_ids))})')
out["filtered_n"] = len(ev2); out["filtered0"] = ev2[:1]
out["requests"] = cl.requests_made
json.dump(out, open("debug_probe.json", "w"), ensure_ascii=False, indent=1, default=str)

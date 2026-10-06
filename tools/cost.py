import json, os, sys, time
sys.path.insert(0, ".")
from wcl_analyzer.api import Cache, WCLClient
from wcl_analyzer.raid import run_raid
cl = WCLClient(os.environ["WCL_CLIENT_ID"], os.environ["WCL_CLIENT_SECRET"], Cache("dbg_cost.sqlite"), verbose=False)
url = "https://www.warcraftlogs.com/reports/39knxwWmHgCj6Bzr"
def spent():
    return float(cl.rate_limit(max_age_s=0)["pointsSpentThisHour"])
out = {}
s0 = spent(); r0 = cl.requests_made
for i in range(3):
    cl.report("39knxwWmHgCj6Bzr", max_age_s=0)
out["poll_3x"] = {"points": spent() - s0, "requests": cl.requests_made - r0}
for name, fid in (("pull_first_on_boss", 29), ("pull_second_same_boss", 30), ("pull_other_boss", 35)):
    s0 = spent(); r0 = cl.requests_made; t0 = time.time()
    run_raid(cl, url, fid, log=lambda m: None, talent_data=[], save_talents=False, mythic=False)
    out[name] = {"points": round(spent() - s0, 1), "requests": cl.requests_made - r0, "secs": round(time.time() - t0, 1)}
out["limit"] = cl.rate_limit(max_age_s=0)
json.dump(out, open("debug_cost.json", "w"), ensure_ascii=False, indent=1)

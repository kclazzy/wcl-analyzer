"""WoW Forever на Warcraft Logs: отвечает ли API forever.warcraftlogs.com и с каким токеном."""
import os, json, requests
cid, sec = os.environ["WCL_CLIENT_ID"], os.environ["WCL_CLIENT_SECRET"]
out = {}
def tok(host):
    r = requests.post(f"https://{host}/oauth/token", data={"grant_type": "client_credentials"}, auth=(cid, sec), timeout=30)
    out[f"token_{host}"] = f"{r.status_code} {r.headers.get('content-type')} {r.text[:150] if 'json' not in (r.headers.get('content-type') or '') else 'json'}"
    try: return r.json().get("access_token")
    except Exception: return None
tw, tf = tok("www.warcraftlogs.com"), tok("forever.warcraftlogs.com")
q = "{worldData{expansions{id name zones{id name difficulties{id name} encounters{id name}}}} reportData{reports(limit:3){data{code title zone{id name} fights{encounterID name difficulty kill}}}} gameData{classes{id name specs{name}}}}"
for name, t in (("forever_tok", tf), ("www_tok", tw)):
    if not t: continue
    for path in ("api/v2/client",):
        r = requests.post(f"https://forever.warcraftlogs.com/{path}", json={"query": q}, headers={"Authorization": "Bearer " + t}, timeout=60)
        try: out[f"api_{name}"] = r.json()
        except Exception: out[f"api_{name}"] = f"{r.status_code} {r.headers.get('content-type')} {r.text[:300]}"
json.dump(out, open("debug_forever.json", "w"), ensure_ascii=False, indent=1)

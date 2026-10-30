"""Где на Warcraft Logs лежат логи WoW Forever: поддомены, зоны, отчёты."""
import os, re, json, socket, requests
cid, sec = os.environ["WCL_CLIENT_ID"], os.environ["WCL_CLIENT_SECRET"]
H = {"User-Agent": "Mozilla/5.0"}
out = {}
subs = set()
for u in ["https://www.warcraftlogs.com/", "https://www.warcraftlogs.com/zone/rankings/1", "https://classic.warcraftlogs.com/"]:
    try:
        t = requests.get(u, headers=H, timeout=30).text
        subs |= set(re.findall(r"https?://([a-z0-9-]+)\.warcraftlogs\.com", t))
        out.setdefault("forever_mentions", []).extend(sorted(set(re.findall(r".{80}[Ff]orever.{80}", t)))[:10])
    except Exception as e:
        out[u] = repr(e)
out["subs_on_pages"] = sorted(subs)
cands = sorted(subs | {"forever", "classicplus", "plus", "era", "vanilla", "fresh", "sod", "classic", "www", "wowforever", "legacy"})
live = []
for s in cands:
    h = f"{s}.warcraftlogs.com"
    try:
        socket.gethostbyname(h)
    except Exception:
        out[h] = "no dns"; continue
    try:
        r = requests.get(f"https://{h}/", headers=H, timeout=30, allow_redirects=False)
        out[h] = f"{r.status_code} {r.headers.get('location','')} title={re.search(r'<title>(.*?)</title>', r.text, re.S).group(1).strip()[:120] if '<title>' in r.text else ''}"
        live.append(s)
    except Exception as e:
        out[h] = repr(e)
q = "{worldData{expansions{id name zones{id name difficulties{id name} encounters{id name}}}}}"
for s in live:
    try:
        tok = requests.post(f"https://{s}.warcraftlogs.com/oauth/token", data={"grant_type": "client_credentials"}, auth=(cid, sec), timeout=30).json()["access_token"]
        r = requests.post(f"https://{s}.warcraftlogs.com/api/v2/client", json={"query": q}, headers={"Authorization": "Bearer " + tok}, timeout=60).json()
        exps = (r.get("data") or {}).get("worldData", {}).get("expansions") or []
        out["api_" + s] = [{"exp": e["id"], "name": e["name"], "zones": [(z["id"], z["name"], [d["name"] for d in z.get("difficulties") or []], [x["name"] for x in z.get("encounters") or []][:12]) for z in (e.get("zones") or [])[-6:]]} for e in exps[-2:]] or r
    except Exception as e:
        out["api_" + s] = repr(e)
json.dump(out, open("debug_forever.json", "w"), ensure_ascii=False, indent=1)

# отчёты Forever: последние публичные по зонам последнего дополнения
try:
    tok = requests.post("https://forever.warcraftlogs.com/oauth/token", data={"grant_type": "client_credentials"}, auth=(cid, sec), timeout=30).json()["access_token"]
    q2 = "{reportData{reports(limit:5){data{code title startTime zone{id name} fights{encounterID name difficulty kill size}}}}}"
    r = requests.post("https://forever.warcraftlogs.com/api/v2/client", json={"query": q2}, headers={"Authorization": "Bearer " + tok}, timeout=60).json()
    out["forever_reports"] = r
    q3 = "{gameData{classes{id name specs{id name}}}}"
    out["forever_classes"] = requests.post("https://forever.warcraftlogs.com/api/v2/client", json={"query": q3}, headers={"Authorization": "Bearer " + tok}, timeout=60).json()
except Exception as e:
    out["forever_reports"] = repr(e)
json.dump(out, open("debug_forever.json", "w"), ensure_ascii=False, indent=1)

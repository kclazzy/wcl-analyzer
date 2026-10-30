"""Wowhead для версий игры: какие пути страниц и dataEnv подсказок отвечают (заклинание 133 — Огненный шар)."""
import json, re, requests
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
out = {}
for p in ["forever", "classic", "mop-classic", "tbc", "cata", "wotlk", "sod", "classic-era"]:
    try:
        r = requests.get(f"https://www.wowhead.com/{p}/ru/spell=133", headers=H, timeout=30, allow_redirects=False)
        out[f"page_{p}"] = f"{r.status_code} {r.headers.get('location','')}"
    except Exception as e:
        out[f"page_{p}"] = repr(e)
for env in range(1, 16):
    try:
        r = requests.get(f"https://nether.wowhead.com/tooltip/spell/133?dataEnv={env}&locale=7", headers=H, timeout=30)
        out[f"env_{env}"] = f"{r.status_code} {(r.json().get('name') if r.headers.get('content-type','').startswith('application/json') else r.text[:80])}"
    except Exception as e:
        out[f"env_{env}"] = repr(e)
for p in ["forever"]:
    try:
        t = requests.get(f"https://www.wowhead.com/{p}", headers=H, timeout=30).text
        out["forever_dataenv"] = sorted(set(re.findall(r"dataEnv[\"']?\s*[:=]\s*(\d+)", t)))[:10]
        out["forever_envs"] = sorted(set(re.findall(r"\"(?:env|dataEnv)\":\s*(\d+)", t)))[:10]
    except Exception as e:
        out["forever_home"] = repr(e)
json.dump(out, open("debug_forever.json", "w"), ensure_ascii=False, indent=1)

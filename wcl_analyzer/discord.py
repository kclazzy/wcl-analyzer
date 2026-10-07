"""Сводка для Discord: короткий текст по боссу и по всему вечеру — вставить в канал гильдии.

Discord принимает до 2000 символов в одном сообщении: длинная сводка вечера делится на несколько сообщений.
Ссылки — в угловых скобках, чтобы Discord не разворачивал под каждой превью.
"""
from __future__ import annotations

import re

LIMIT = 1900                 # с запасом до 2000 символов Discord
BOSS_LINES = 6               # строк «главного» по боссу
EVENING_LINES = 2            # строк на босса в сводке вечера
_TAB = re.compile(r"\s*\((?:подробно — )?во вкладке «[^»]+»\)|\s*—\s*очередь для MRT во вкладке «[^»]+»")
_MD = re.compile(r"([*_~`|>])")


def _clean(line: str) -> str:
    """Без отсылок к вкладкам программы (в Discord их нет) и без случайной разметки Discord в именах."""
    return _MD.sub(r"\\\1", _TAB.sub("", line or "")).strip()


_QUOTE = re.compile(r"«([^»]+)»")


def ability_links(R: dict, master: dict) -> dict:
    """{название способности: ссылка} для всех «…» в главном по бою и «Кому что поправить»:
    гайд Mythic Trap (как на странице), иначе Wowhead по номеру способности из лога."""
    from .wowhead import page_url
    texts = list(R.get("brief") or []) + [i.get("text", "") for i in R.get("issues") or []]
    want = {m for t in texts for m in _QUOTE.findall(t or "")}
    guide = R.get("guide_links") or {}
    ids: dict = {}
    for a in master.get("abilities") or []:
        n, gid = a.get("name"), a.get("gameID")
        if n in want and gid and int(gid) > 1:
            ids.setdefault(n, int(gid))   # одинаковое имя у разных номеров — первый, как на странице
    out = {}
    for n in want:
        if guide.get(n):
            out[n] = guide[n]
        elif n in ids:
            out[n] = page_url(ids[n])
        else:
            m = re.search(r"#(\d+)$", n)   # «Unknown Ability #123»
            if m:
                out[n] = page_url(int(m.group(1)))
    return out


def _linked(line: str, links: dict, preview: bool = True) -> str:
    """Строка для Discord: «Название» — ссылкой (preview=False — в угловых скобках, без карточки-превью
    под сообщением); остальной текст — без случайной разметки."""
    out = []
    for k, part in enumerate(_QUOTE.split(line or "")):
        if k % 2 == 0:
            out.append(_MD.sub(r"\\\1", part))   # без обрезки пробелов: они стоят вокруг ссылок
        else:
            name, url = _MD.sub(r"\\\1", part), (links or {}).get(part)
            out.append(f"«[{name}]({url if preview else '<' + url + '>'})»" if url else f"«{name}»")
    return _TAB.sub("", "".join(out)).strip()


def _outcome(I: dict) -> str:
    if I.get("kill"):
        return "килл"
    pct = I.get("boss_pct")
    return f"вайп, босс на {pct:.1f}%".replace(".", ",") if pct is not None else "вайп"


def boss_text(R: dict, lines: int = BOSS_LINES) -> str:
    """Один бой: заголовок, исход, «главное» и ссылка на лог."""
    I, S = R.get("info") or {}, R.get("summary") or {}
    head = f"**{_clean(I.get('boss', ''))}** — {I.get('difficulty', '')}, {_outcome(I)}, {I.get('duration', '')}"
    if S.get("pull_n") and S.get("pulls"):
        head += f" (пулл {S['pull_n']} из {S['pulls']})"
    out = [head]
    links = (R.get("extras") or {}).get("ability_links") or {}
    for line in (R.get("brief") or [])[:lines]:
        out.append("• " + _linked(line, links, preview=False))
    if I.get("url"):
        out.append(f"<{I['url']}>")
    return _fit("\n".join(out))


def evening_messages(S: dict) -> list[str]:
    """Сводка вечера (все боссы): по боссу — исход и две главные строки; на несколько сообщений, если длинно."""
    I = S.get("info") or {}
    head = f"**{_clean(I.get('title') or I.get('zone') or 'Рейд')}** — итоги вечера"
    if I.get("difficulty"):
        head += f" ({I['difficulty']})"
    blocks = []
    for b in S.get("bosses") or []:
        res = "килл" if b.get("kill") else (f"вайп, босс на {b['boss_pct']:.1f}%".replace(".", ",") if b.get("boss_pct") is not None else "вайп")
        pulls = f", пуллов: {b['pulls']}" if b.get("pulls") else ""
        lines = [f"**{_clean(b.get('boss', ''))}** — {res}, {b.get('duration', '')}{pulls}"]
        lines += ["• " + _linked(x, b.get("links") or {}, preview=False) for x in (b.get("brief") or [])[:EVENING_LINES]]
        blocks.append("\n".join(lines))
    for s in S.get("skipped") or []:
        blocks.append(f"**{_clean(s.get('boss', ''))}** — не разобран: {_clean(s.get('reason', ''))}")
    tail = f"<{I['url']}>" if I.get("url") else ""
    msgs, cur = [], head
    for blk in blocks:
        if len(cur) + 2 + len(blk) > LIMIT:
            msgs.append(cur)
            cur = blk
        else:
            cur += "\n\n" + blk
    if tail:
        if len(cur) + 1 + len(tail) > LIMIT:
            msgs.append(cur)
            cur = tail
        else:
            cur += "\n" + tail
    msgs.append(cur)
    return [_fit(m) for m in msgs]


def _fit(text: str) -> str:
    if len(text) <= LIMIT:
        return text
    cut = text[:LIMIT - 1]
    return cut[:cut.rfind("\n")] + "\n…" if "\n" in cut else cut + "…"


# ------------------------------------------------------------ вебхук: карточка пулла для живого лога
EMBED_DESC = 4000     # пределы Discord: описание до 4096, поле до 1024, всё сообщение до 6000 символов
FIELD = 1000
EMBED_TOTAL = 5800
TOP_ISSUES = 5        # как в «Кому что поправить» на странице: по одному, самому важному пункту на игрока
COLOR_KILL, COLOR_WIPE = 0x2ECC71, 0xE67E22
WEBHOOK_RE = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d{5,25}/[\w-]{20,200}$")


def top_issues(issues: list[dict], n: int = TOP_ISSUES) -> list[dict]:
    seen, out = set(), []
    for i in issues or []:
        if int(i.get("severity") or 0) >= 4 and i.get("player") not in seen:
            seen.add(i.get("player"))
            out.append(i)
    return out[:n]


def _who(i: dict) -> str:
    from .names_ru import spec_ru
    w = spec_ru(i.get("cls") or "", i.get("spec") or "")   # уже с классом: «Тьма (жрец)»
    return ", ".join(x for x in (i.get("role") or "", w) if x)


def pull_embed(R: dict, issues: bool = True) -> dict:
    """Карточка пулла для вебхука Discord: заголовок со ссылкой на бой, цвет по исходу, главное по пуллу,
    «Кому что поправить» (если issues) и номер пулла. Упоминания (@everyone, роли, игроки) выключены."""
    I, S = R.get("info") or {}, R.get("summary") or {}
    title = f"{I.get('boss', '')} — {I.get('difficulty', '')}, {_outcome(I)}, {I.get('duration', '')}"
    links = (R.get("extras") or {}).get("ability_links") or {}
    lines = ["• " + _linked(x, links) for x in (R.get("brief") or [])[:BOSS_LINES]]
    desc = ""
    for ln in lines:
        if len(desc) + len(ln) + 1 > EMBED_DESC:
            break
        desc += ("\n" if desc else "") + ln
    emb = {"title": title[:250], "color": COLOR_KILL if I.get("kill") else COLOR_WIPE, "description": desc or "Явных проблем не найдено."}
    if I.get("url"):
        emb["url"] = I["url"]
    fields = []
    if issues:
        cur = ""
        for i in top_issues(R.get("issues") or []):
            item = f"**{_clean(i.get('player', ''))}** — {_clean(_who(i))}\n{_linked(i.get('text', ''), links)}"
            if len(item) > FIELD:   # длинная строка — без ссылок, но целиком (обрезанная ссылка ломает разметку)
                item = f"**{_clean(i.get('player', ''))}** — {_clean(_who(i))}\n{_clean(i.get('text', ''))}"[:FIELD]
            if cur and len(cur) + 2 + len(item) > FIELD:
                fields.append(cur)
                cur = item
            else:
                cur += ("\n\n" if cur else "") + item
        if cur:
            fields.append(cur)
    emb["fields"] = [{"name": "Кому что поправить" if k == 0 else "\u200b", "value": v} for k, v in enumerate(fields[:5])]
    foot = "WCL Analyzer"
    if S.get("pull_n"):
        foot += f" · пулл {S['pull_n']}" + (f" из {S['pulls']}" if S.get("pulls") else "")
    emb["footer"] = {"text": foot}
    while len(emb["title"]) + len(emb["description"]) + sum(len(f["name"]) + len(f["value"]) for f in emb["fields"]) \
            + len(foot) > EMBED_TOTAL and emb["fields"]:
        emb["fields"].pop()
    return {"username": "WCL Analyzer", "embeds": [emb], "allowed_mentions": {"parse": []}}


def check_webhook(url: str) -> str:
    url = (url or "").strip()
    if not WEBHOOK_RE.match(url):
        raise ValueError("Это не ссылка вебхука Discord: она начинается с https://discord.com/api/webhooks/…")
    return url


def send_webhook(url: str, payload: dict, timeout: float = 15) -> None:
    """Отправить сообщение в канал. Упоминания выключены всегда, что бы ни пришло в тексте."""
    import requests
    url = check_webhook(url)
    payload = {**payload, "allowed_mentions": {"parse": []}}
    try:
        r = requests.post(url, json=payload, timeout=timeout, params={"wait": "true"})
    except requests.RequestException as e:
        raise RuntimeError(f"Discord не ответил: {e.__class__.__name__}") from None
    if r.status_code == 429:
        raise RuntimeError("Discord просит подождать (слишком много сообщений) — следующее уйдёт позже")
    if r.status_code in (401, 403, 404):
        raise RuntimeError("Discord не принял вебхук: ссылка неверная или вебхук удалён")
    if r.status_code >= 400:
        raise RuntimeError(f"Discord вернул ошибку {r.status_code}: {r.text[:200]}")


def test_payload() -> dict:
    return {"username": "WCL Analyzer", "allowed_mentions": {"parse": []},
            "embeds": [{"title": "WCL Analyzer подключён", "color": COLOR_KILL,
                        "description": "Сюда будут приходить итоги каждого пулла, пока включена отправка в Discord."}]}

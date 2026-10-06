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
    for line in (R.get("brief") or [])[:lines]:
        out.append("• " + _clean(line))
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
        lines += ["• " + _clean(x) for x in (b.get("brief") or [])[:EVENING_LINES]]
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

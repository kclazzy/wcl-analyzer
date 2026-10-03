"""BattleEvent — нормализованное событие боя.

Интерфейс не зависит от сырых событий Warcraft Logs: всё, что видит страница «Бой по шагам»,
собрано в эти объекты (и в данные карты из movement.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Типы событий ленты и их подписи
TYPE_RU = {
    "opener": "Опенер", "phase": "Фаза", "mechanic": "Механика", "movement": "Движение",
    "burst": "Бурст", "cooldown": "Кулдаун", "defensive": "Защита", "potion": "Зелье",
    "switch": "Смена цели", "death": "Смерть", "idle": "Простой", "end": "Конец боя",
}
# Фильтры страницы: к какой группе относится тип события
TYPE_FILTER = {
    "opener": "burst", "phase": "mechanics", "mechanic": "mechanics", "movement": "movement",
    "burst": "burst", "cooldown": "cooldowns", "defensive": "defensives", "potion": "burst",
    "switch": "switch", "death": "errors", "idle": "errors", "end": "mechanics",
}
# Базовая значимость типа (0–100): обязательные события — 100
BASE_SCORE = {
    "opener": 100, "phase": 100, "death": 100, "end": 100,
    "burst": 70, "defensive": 55, "mechanic": 30, "movement": 40, "switch": 45,
    "cooldown": 35, "potion": 30, "idle": 30,
}
MUST = {"opener", "phase", "death", "end"}
SEVERITY_BONUS = {"high": 30, "medium": 15, "low": 0, None: 0}


@dataclass
class Line:
    """Строка карточки: значок + короткий текст. kind — для цвета (bad/good/top/muted)."""
    icon: str
    text: str
    kind: str = ""

    def to_dict(self) -> dict:
        return {"icon": self.icon, "text": self.text, "kind": self.kind}


@dataclass
class BattleEvent:
    t: float                              # начало, секунды от пулла
    type: str                             # ключ из TYPE_RU
    title: str = ""                       # заголовок карточки (без времени)
    end: float | None = None              # конец (окна бурста, движения)
    subtype: str = ""
    ability: str = ""                     # способность игрока
    mechanic: str = ""                    # механика босса
    lines: list[Line] = field(default_factory=list)
    severity: str | None = None           # high / medium / low — отклонение от топа
    score: float = 0.0                    # значимость для отбора
    movement: int | None = None           # номер движения на карте
    window: tuple[float, float] | None = None  # окно карты (что показать при выборе события)
    top: dict = field(default_factory=dict)    # медиана топа для сравнения
    diff: dict = field(default_factory=dict)   # разница с топом
    phase: int | None = None
    merged: list[str] = field(default_factory=list)  # типы событий, вошедших в карточку

    def finalize_score(self) -> float:
        self.score = BASE_SCORE.get(self.type, 20) + SEVERITY_BONUS.get(self.severity, 0) + self.score
        return self.score

    def to_dict(self) -> dict:
        types = [self.type] + [m for m in self.merged if m != self.type]
        return {
            "t": round(self.t, 1), "end": round(self.end, 1) if self.end is not None else None,
            "type": self.type, "type_ru": TYPE_RU.get(self.type, self.type), "title": self.title,
            "subtype": self.subtype, "ability": self.ability, "mechanic": self.mechanic,
            "lines": [ln.to_dict() for ln in self.lines], "severity": self.severity,
            "movement": self.movement,
            "window": [round(self.window[0], 1), round(self.window[1], 1)] if self.window else None,
            "top": self.top, "diff": self.diff, "phase": self.phase,
            "filters": sorted({TYPE_FILTER.get(x, "mechanics") for x in types}),
            "types": types,
        }

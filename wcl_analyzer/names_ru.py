"""Русские названия классов и специализаций (как в русском клиенте WoW) — только для показа.

В запросах к Warcraft Logs остаются английские названия: API понимает только их.
"""
from __future__ import annotations

CLASSES = {
    "DeathKnight": "рыцарь смерти", "DemonHunter": "охотник на демонов", "Druid": "друид",
    "Evoker": "пробудитель", "Hunter": "охотник", "Mage": "маг", "Monk": "монах", "Paladin": "паладин",
    "Priest": "жрец", "Rogue": "разбойник", "Shaman": "шаман", "Warlock": "чернокнижник", "Warrior": "воин",
}
SPECS = {
    ("DeathKnight", "Blood"): "Кровь", ("DeathKnight", "Frost"): "Лёд", ("DeathKnight", "Unholy"): "Нечестивость",
    ("DemonHunter", "Havoc"): "Истребление", ("DemonHunter", "Vengeance"): "Месть",
    ("Druid", "Balance"): "Баланс", ("Druid", "Feral"): "Сила зверя", ("Druid", "Guardian"): "Страж",
    ("Druid", "Restoration"): "Исцеление",
    ("Evoker", "Devastation"): "Опустошение", ("Evoker", "Preservation"): "Сохранение",
    ("Evoker", "Augmentation"): "Насыщение",
    ("Hunter", "BeastMastery"): "Повелитель зверей", ("Hunter", "Marksmanship"): "Стрельба",
    ("Hunter", "Survival"): "Выживание",
    ("Mage", "Arcane"): "Тайная магия", ("Mage", "Fire"): "Огонь", ("Mage", "Frost"): "Лёд",
    ("Monk", "Brewmaster"): "Хмелевар", ("Monk", "Mistweaver"): "Ткач туманов", ("Monk", "Windwalker"): "Танцующий с ветром",
    ("Paladin", "Holy"): "Свет", ("Paladin", "Protection"): "Защита", ("Paladin", "Retribution"): "Воздаяние",
    ("Priest", "Discipline"): "Послушание", ("Priest", "Holy"): "Свет", ("Priest", "Shadow"): "Тьма",
    ("Rogue", "Assassination"): "Ликвидация", ("Rogue", "Outlaw"): "Головорез", ("Rogue", "Subtlety"): "Скрытность",
    ("Shaman", "Elemental"): "Стихии", ("Shaman", "Enhancement"): "Совершенствование",
    ("Shaman", "Restoration"): "Исцеление",
    ("Warlock", "Affliction"): "Колдовство", ("Warlock", "Demonology"): "Демонология",
    ("Warlock", "Destruction"): "Разрушение",
    ("Warrior", "Arms"): "Оружие", ("Warrior", "Fury"): "Неистовство", ("Warrior", "Protection"): "Защита",
}


def _key(x: str) -> str:
    return (x or "").replace(" ", "").replace("-", "")


def cls_ru(cls: str) -> str:
    return CLASSES.get(_key(cls), cls or "")


def spec_ru(cls: str, spec: str) -> str:
    """«Лёд (маг)». Неизвестное название показывается как есть."""
    c = cls_ru(cls)
    s = SPECS.get((_key(cls), _key(spec)), spec or "")
    return f"{s} ({c})" if s and c else (s or c)

# src/class_colors.py
"""Standard WoW class colors, keyed by the space-separated class display name."""

CLASS_COLORS: dict[str, str] = {
    "Death Knight": "#C41E3A",
    "Demon Hunter": "#A330C9",
    "Druid": "#FF7C0A",
    "Evoker": "#33937F",
    "Hunter": "#AAD372",
    "Mage": "#3FC7EB",
    "Monk": "#00FF98",
    "Paladin": "#F48CBA",
    "Priest": "#FFFFFF",
    "Rogue": "#FFF468",
    "Shaman": "#0070DD",
    "Warlock": "#8788EE",
    "Warrior": "#C69B6D",
}


def class_color(class_name: str) -> str | None:
    """Look up the class color for a display name like 'Demon Hunter'."""
    return CLASS_COLORS.get(class_name)

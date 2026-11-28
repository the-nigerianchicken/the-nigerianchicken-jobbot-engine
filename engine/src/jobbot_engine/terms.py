"""Which term a posting is for, read from its own words. Ported unchanged from the old engine."""

from __future__ import annotations

import re

SEASON_OF_MONTH = {
    1: "Winter", 2: "Winter", 3: "Winter", 4: "Winter", 5: "Summer", 6: "Summer",
    7: "Summer", 8: "Summer", 9: "Fall", 10: "Fall", 11: "Fall", 12: "Fall",
}  # fmt: skip
_MONTH_NAMES = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
MONTHS = {m: i for i, m in enumerate(_MONTH_NAMES, start=1)}
TERM_ANY = re.compile(
    r"\b(?:(summer|winter|fall|autumn|spring)\s*'?\s*(20\d\d|\d\d)\b"
    r"|(20\d\d)\s+(summer|winter|fall|autumn|spring)\b"
    r"|(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?,?\s+(20\d\d)\b)",
    re.I,
)

# A date after these words is when the student graduates, not the term:
# "graduating between Fall 2028 and Summer 2029".
NOT_TERM = re.compile(
    r"graduat|degree|class of|complet|enrolled|pursuing|expected|returning to|remaining", re.I
)

# When each term ends and starts, as a month. Spring is given Summer's end because some employers use it for
# a May start, so the doubt goes the person's way.
TERM_ENDS = {"winter": 4, "spring": 8, "summer": 8, "fall": 12}
TERM_STARTS = {"winter": 1, "spring": 1, "summer": 5, "fall": 9}


def term_of(*texts: str | None) -> list[str]:
    """The terms a posting is for, from the first text that names any: "Summer 2027", "2027 Summer", or a
    start month ("January 2027" is Winter). At most three, between 2025 and 2029."""
    for t in texts:
        found: list[str] = []
        text = (t or "")[:6000]
        for m in TERM_ANY.finditer(text):
            if NOT_TERM.search(text[max(0, m.start() - 90) : m.start()]):
                continue
            if m.group(1):
                season, year = m.group(1), m.group(2)
            elif m.group(3):
                season, year = m.group(4), m.group(3)
            else:
                season, year = SEASON_OF_MONTH[MONTHS[m.group(5)[:3].lower()]], m.group(6)
            season = "Fall" if season.lower() == "autumn" else season.title()
            year = year if len(year) == 4 else "20" + year
            if not 2025 <= int(year) <= 2029:
                continue
            name = f"{season} {year}"
            if name not in found:
                found.append(name)
        if found:
            return found[:3]
    return []

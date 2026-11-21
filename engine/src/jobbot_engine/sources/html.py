"""Descriptions as readable text: tags out, structure kept.

Paragraphs and list items stay on their own lines, because he reads these descriptions in the app, and a
posting flattened to one line is unreadable.
"""

from __future__ import annotations

import html as htmlmod
import re

# Sites that escape their own descriptions twice leave "&nbsp;" written out in the text. Only these harmless
# ones are undone: a second full unescape would turn an escaped "&amp;lt;script&amp;gt;" back into a tag.
_HARMLESS_NAMES = (
    *("nbsp", "amp", "rsquo", "lsquo", "ldquo", "rdquo", "mdash", "ndash"),
    *("hellip", "bull", "middot", "deg", "reg", "trade", "copy"),
)
_HARMLESS = {name: " " if name == "nbsp" else htmlmod.unescape(f"&{name};") for name in _HARMLESS_NAMES}
_ENTITY = re.compile(r"&(" + "|".join(_HARMLESS) + r");")


def strip(html: str) -> str:
    text = htmlmod.unescape(html or "")
    text = _ENTITY.sub(lambda m: _HARMLESS[m.group(1)], text)
    text = re.sub(r"(?i)<\s*li[^>]*>", "\n• ", text)
    text = re.sub(r"(?i)<\s*(br|/p|/div|/li|/ul|/ol|/h[1-6]|/tr|/section)\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

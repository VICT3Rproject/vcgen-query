"""Company groups for recorded SEND animal-supplier names (TS ``SPLRNAM``).

A supplier name records the company and often a breeding site
(``CHARLES RIVER LABORATORIES, USA, RALEIGH``). The group is the company: animals
of one company share breeding stock and husbandry across its sites. Harlan,
Envigo and Inotiv are one company under three names (Harlan renamed itself Envigo
in 2015; Inotiv bought Envigo in 2021), and Iffa Credo became Charles River
France. A name of another company keeps its own normalized label.
"""

from __future__ import annotations

import re


_COMPANIES = (
    (("CHARLES RIVER", "IFFA CREDO"), "charles_river"),
    (("ENVIGO", "HARLAN", "INOTIV"), "envigo"),
    (("VITAL RIVER",), "vital_river"),
)
_PLACEHOLDERS = frozenset({"", "UNKNOWN", "OTHER", "NA", "N A", "NONE", "NOT APPLICABLE"})


def _label(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", raw.strip().upper()).strip()


def supplier_group(raw: str) -> str | None:
    """Return the company group of one supplier name, or None for a placeholder."""
    label = _label(raw)
    if label in _PLACEHOLDERS:
        return None
    for tokens, group in _COMPANIES:
        if any(token in label for token in tokens):
            return group
    company = _label(raw.split(",", 1)[0])
    if company in _PLACEHOLDERS:
        return None
    return f"label:{company.lower().replace(' ', '_')}"

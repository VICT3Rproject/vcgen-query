"""Stable categorical labels for recorded SEND test-facility names."""

from __future__ import annotations

import re


FACILITY_GROUP_CONTRACT = "vcgen.facility_group.v1"
_GROUP_PREFIXES = ("site:", "entity:", "opaque:", "label:")


def lexical_facility_label(raw: str) -> str:
    """Consolidate case, punctuation, spacing, and the known BIPKG suffix."""
    value = re.sub(r"\s*\(BIPKG\)\s*$", "", raw.strip().upper())
    return re.sub(r"[^A-Z0-9]+", " ", value).strip()


def facility_group(raw: str) -> tuple[str, str, str]:
    """Return category, evidence type, and matching rule for a source label."""
    label = lexical_facility_label(raw)
    if not label:
        raise ValueError("A facility group needs a nonempty source label.")
    if "CHARLES RIVER" in label:
        for site in ("SHERBROOKE", "EDINBURGH", "MONTREAL", "DEN BOSCH", "ASHLAND"):
            if site in label:
                key = site.lower().replace(" ", "_")
                return f"site:charles_river:{key}", "explicit_site", f"charles_river_{key}"
    for token, key in (("BEERSE", "beerse"), ("SPRING HOUSE", "spring_house")):
        if token in label:
            return f"site:{key}", "explicit_site", f"place_{key}"
    if label.startswith("BOEHRINGER INGELHEIM PHARMA GMBH"):
        return "entity:boehringer_ingelheim_pharma", "entity_only", "boehringer_legal_name"
    for organization, site in (
        ("APTUIT", "VERONA"),
        ("WUXI APPTEC", "SUZHOU"),
        ("SANOFI", "MONTPELLIER"),
        ("MERCK", "DARMSTADT"),
    ):
        if organization in label and site in label:
            key = f"{organization.lower().replace(' ', '_')}:{site.lower()}"
            return f"site:{key}", "explicit_site", f"place_{key.replace(':', '_')}"
    if re.fullmatch(r"[A-Z]\d{3}", label):
        return f"opaque:{label.lower()}", "opaque_code", "opaque_code"
    return f"label:{label.lower().replace(' ', '_')}", "unresolved_label", "lexical_only"


def canonical_facility_group(value: str) -> str:
    """Accept a category or normalize a recorded facility name to one."""
    normalized = value.strip().lower()
    if normalized.startswith(_GROUP_PREFIXES):
        return normalized
    return facility_group(value)[0]

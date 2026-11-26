"""Recognise a control arm by its DM ARM text.

A study without EX rows is a control-only export only when its animals are
controls. Many such studies still have dosed arms in DM ARM, so the TS exposure
fallback of `subject_exposure_summary` applies only to a subject whose arm is
empty or a control arm.

The rule fails closed. An arm is a control arm when its text has no nonzero
dose and it has a control word (CONTROL, VEHICLE, PLACEBO, or the German
VEHIKEL, KONTROLLE, KONT) that is not POSITIVE CONTROL, or it has a dose and
every dose is zero (`0 mg/kg/day`, `0.0 mg/kg`). A dose is a number followed by
a mass or amount unit (MG, UG, MCG, NG, G, MOL, MMOL, UMOL, IU, MBQ, GY). A
volume or a concentration (`10 mL/kg`, `0.5%`) is not a dose: a vehicle arm
can state them. Every other arm text (`ACC-None`, `2.5 ml/kg/day`, `ACC-1`) is
not a control arm.

Reviewed on 2026-11-20 against the ARM values of the studies without EX rows in
the reference database of source `6b1a42fc`.
"""

from __future__ import annotations

# A control word starts a word; it can run into a group code (`Vehicle controlM1`).
# KONT must end its word: KONTRAST is not a control word.
_CONTROL_WORD = r"(^|[^A-Z])(CONTROL|VEHICLE|PLACEBO|VEHIKEL|KONTROLLE|KONT([^A-Z]|$))"
_POSITIVE_CONTROL = r"POSITIVE[^A-Z]*CONTROL"
_DOSE = r"([0-9]+(?:[.,][0-9]+)?) ?(?:MG|UG|µG|ΜG|MCG|NG|G|MOL|MMOL|UMOL|IU|MBQ|GY)([^A-Z]|$)"


def _literal(pattern: str) -> str:
    return "'" + pattern.replace("'", "''") + "'"


def control_arm_sql(arm: str) -> str:
    """Return a SQL condition that is true when the arm expression `arm` is empty or a control arm."""

    text = f"upper(trim(CAST({arm} AS VARCHAR)))"
    doses = f"regexp_extract_all({text}, {_literal(_DOSE)}, 1)"
    nonzero = f"len(list_filter({doses}, dose -> coalesce(TRY_CAST(replace(dose, ',', '.') AS DOUBLE), 1) <> 0))"
    return (
        f"(nullif({text}, '') IS NULL OR ({nonzero} = 0 AND ("
        f"(regexp_matches({text}, {_literal(_CONTROL_WORD)}) "
        f"AND NOT regexp_matches({text}, {_literal(_POSITIVE_CONTROL)})) "
        f"OR len({doses}) > 0)))"
    )

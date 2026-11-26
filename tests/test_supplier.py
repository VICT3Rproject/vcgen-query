"""The company group of the animal supplier (TS SPLRNAM).

Failure modes:
- The sites of one company (Charles River in Raleigh and in Sulzfeld) become
  separate groups, so a study effect of the company is split by site.
- Harlan and Envigo (one company that renamed itself) become two groups.
- A placeholder (UNKNOWN, OTHER, blank) becomes a group that unrelated
  studies share.
- A study with the animals of two companies gets one of them at random.
"""

import pytest

from vcgen_query.cohort import _single_supplier_group
from vcgen_query.supplier import supplier_group


@pytest.mark.parametrize(
    "raw, group",
    [
        ("CHARLES RIVER LABORATORIES, USA, RALEIGH", "charles_river"),
        ("Charles River Laboratories, DEU, Sulzfeld", "charles_river"),
        ("IFFA CREDO", "charles_river"),
        ("ENVIGO, US, INDIANAPOLIS", "envigo"),
        ("HARLAN LABORATORIES, NLD, HORST", "envigo"),
        ("HARLAN RCC", "envigo"),
        ("INOTIV, USA, INDIANAPOLIS", "envigo"),
        ("VITAL RIVER LABORATORY ANIMAL TECHNOLOGY, CHN, BEIJING", "vital_river"),
        ("JANVIER LABS", "label:janvier_labs"),
        ("TACONIC, USA, UNKNOWN", "label:taconic"),
        ("UNKNOWN", None),
        ("Other", None),
        ("  ", None),
        ("", None),
        ("?", None),
    ],
)
def test_a_supplier_name_has_one_group(raw: str, group: str | None) -> None:
    assert supplier_group(raw) == group


def test_a_study_with_two_companies_has_no_group() -> None:
    assert _single_supplier_group(["CHARLES RIVER LABORATORIES, USA, RALEIGH",
                                   "CHARLES RIVER LABORATORIES, GBR, MARGATE"]) == "charles_river"
    assert _single_supplier_group(["CHARLES RIVER LABORATORIES, USA, RALEIGH",
                                   "ENVIGO, US, INDIANAPOLIS"]) is None
    assert _single_supplier_group(["ENVIGO, US, INDIANAPOLIS", "UNKNOWN"]) == "envigo"
    assert _single_supplier_group([]) is None

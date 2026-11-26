"""Execute the bundled notebooks against a synthetic SEND source.

Both notebooks must run headless to successful completion as part of the
package test suite. The synthetic CSV directory satisfies the bundled
Wistar Han 28-day predicate and the editable-query example, so no real
archive is required.
"""

from __future__ import annotations

import csv
from pathlib import Path

import nbformat
import pytest
from nbclient import NotebookClient

_NOTEBOOKS = Path(__file__).resolve().parents[1] / "notebooks"

_DM_COLUMNS = (
    "STUDYID,DOMAIN,USUBJID,VICT3RID,SPECIES,STRAIN,SBSTRAIN,SEX,"
    "AGE,AGETXT,AGEU,ARMCD,ARM,SETCD,RPATHCD,RFSTDTC"
).split(",")
_TS_COLUMNS = "STUDYID,TSSEQ,TSPARMCD,TSPARM,TSVAL".split(",")
_EX_COLUMNS = (
    "STUDYID,USUBJID,EXSEQ,EXTRT,EXDOSE,EXDOSTXT,EXDOSU,EXDOSFRM,"
    "EXDOSFRQ,EXROUTE,EXMETHOD,EXTRTV,EXSTDY,EXENDY"
).split(",")
_SE_COLUMNS = "STUDYID,USUBJID,SESEQ,ETCD,ELEMENT,SESTDTC,SEENDTC".split(",")
_BW_COLUMNS = (
    "STUDYID,USUBJID,BWTESTCD,BWTEST,BWDY,BWDTC,BWSTRESN,BWSTRESU,BWORRES,BWORRESU,BWBLFL"
).split(",")
_LB_COLUMNS = (
    "STUDYID,USUBJID,LBTESTCD,LBTEST,LBDY,LBDTC,LBSEQ,LBBLFL,LBEXCLFL,"
    "LBSTRESN,LBSTRESU,LBORRES,LBORRESU,LBSPEC"
).split(",")

_SUBJECTS = (
    {"study_id": "S1", "subject_id": "0001", "sex": "M"},
    {"study_id": "S1", "subject_id": "0002", "sex": "F"},
)


@pytest.fixture(scope="session")
def synthetic_send_dir(tmp_path_factory):
    """Write one minimal SEND CSV source that both notebooks can query."""

    send_dir = tmp_path_factory.mktemp("send_source")
    _write_csv(send_dir / "dm.csv", _DM_COLUMNS, _dm_rows())
    _write_csv(send_dir / "ts.csv", _TS_COLUMNS, _ts_rows())
    _write_csv(send_dir / "ex.csv", _EX_COLUMNS, _ex_rows())
    _write_csv(send_dir / "se.csv", _SE_COLUMNS, _se_rows())
    _write_csv(send_dir / "bw.csv", _BW_COLUMNS, _bw_rows())
    _write_csv(send_dir / "lb.csv", _LB_COLUMNS, _lb_rows())
    return send_dir


def _dm_rows():
    return [
        {
            "STUDYID": s["study_id"],
            "DOMAIN": "DM",
            "USUBJID": f"{s['study_id']}-{s['subject_id']}",
            "VICT3RID": "VID",
            "SPECIES": "RAT",
            "STRAIN": "WISTAR HAN",
            "SBSTRAIN": "",
            "SEX": s["sex"],
            "AGE": "8",
            "AGETXT": "",
            "AGEU": "WEEK",
            "ARMCD": "CTRL",
            "ARM": "CONTROL",
            "SETCD": "1",
            "RPATHCD": "",
            "RFSTDTC": "2020-01-01",
        }
        for s in _SUBJECTS
    ]


def _ts_rows():
    return [
        {
            "STUDYID": "S1",
            "TSSEQ": "1",
            "TSPARMCD": "DOSDUR",
            "TSPARM": "Study Length",
            "TSVAL": "28",
        }
    ]


def _ex_rows():
    return [
        {
            "STUDYID": s["study_id"],
            "USUBJID": f"{s['study_id']}-{s['subject_id']}",
            "EXSEQ": "1",
            "EXTRT": "0.5% METHOCEL",
            "EXDOSE": "0",
            "EXDOSTXT": "",
            "EXDOSU": "MG/KG",
            "EXDOSFRM": "SOLUTION",
            "EXDOSFRQ": "QD",
            "EXROUTE": "ORAL GAVAGE",
            "EXMETHOD": "GAVAGE",
            "EXTRTV": "0.5% METHOCEL",
            "EXSTDY": "1",
            "EXENDY": "28",
        }
        for s in _SUBJECTS
    ]


def _se_rows():
    return [
        {
            "STUDYID": s["study_id"],
            "USUBJID": f"{s['study_id']}-{s['subject_id']}",
            "SESEQ": "1",
            "ETCD": "CTRL",
            "ELEMENT": "CONTROL",
            "SESTDTC": "2020-01-01",
            "SEENDTC": "2020-02-15",
        }
        for s in _SUBJECTS
    ]


def _bw_rows():
    return [
        {
            "STUDYID": s["study_id"],
            "USUBJID": f"{s['study_id']}-{s['subject_id']}",
            "BWTESTCD": "BW",
            "BWTEST": "BODY WEIGHT",
            "BWDY": str(day),
            "BWDTC": "2020-01-01T08:00:00",
            "BWSTRESN": str(value),
            "BWSTRESU": "g",
            "BWORRES": str(value),
            "BWORRESU": "g",
            "BWBLFL": "Y" if day == 1 else "",
        }
        for s in _SUBJECTS
        for day, value in ((1, 250), (7, 270), (14, 290), (28, 320))
    ]


def _lb_rows():
    return [
        {
            "STUDYID": s["study_id"],
            "USUBJID": f"{s['study_id']}-{s['subject_id']}",
            "LBTESTCD": "HCT",
            "LBTEST": "HEMATOCRIT",
            "LBDY": str(day),
            "LBDTC": "2020-01-01T08:00:00",
            "LBSEQ": str(index),
            "LBBLFL": "Y" if day == 1 else "",
            "LBEXCLFL": "",
            "LBSTRESN": str(value),
            "LBSTRESU": "%",
            "LBORRES": str(value),
            "LBORRESU": "%",
            "LBSPEC": "BLOOD",
        }
        for s in _SUBJECTS
        for index, (day, value) in enumerate(((1, 0.45), (28, 0.48)), start=1)
    ]


def _write_csv(path, columns, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


@pytest.mark.parametrize(
    "notebook_name",
    ["02_editable_cohort_query.ipynb"],
)
def test_notebook_runs_to_completion(notebook_name, synthetic_send_dir, tmp_path, monkeypatch):
    notebook_path = _NOTEBOOKS / notebook_name
    assert notebook_path.is_file(), f"missing bundled notebook: {notebook_path}"

    monkeypatch.setenv("VCGEN_SEND_ARCHIVE", str(synthetic_send_dir))
    monkeypatch.chdir(tmp_path)

    notebook = nbformat.read(notebook_path, as_version=4)
    NotebookClient(
        notebook,
        timeout=600,
        kernel_name="python3",
        resources={"metadata": {"path": str(tmp_path)}},
    ).execute()

    code_cells = [cell for cell in notebook.cells if cell.cell_type == "code"]
    assert code_cells, "notebook must contain code cells"
    for cell in code_cells:
        assert cell.get("execution_count") is not None, (
            f"cell was not executed in {notebook_name}: "
            f"{cell.source.splitlines()[0] if cell.source else cell!r}"
        )
        errors = [
            output for output in cell.get("outputs", []) if output.get("output_type") == "error"
        ]
        assert not errors, f"cell error in {notebook_name}: {errors}"

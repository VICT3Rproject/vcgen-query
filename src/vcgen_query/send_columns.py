"""SEND-like column constants for VICT3R cohort queries.

This module is the package-private column vocabulary. It is a strict subset of
the host project's shared constants: the repository-specific path defaults
(``DEFAULT_ARCHIVE`` / ``DEFAULT_EXTRACT_DIR`` / ``DEFAULT_DUCKDB_PATH``) are
deliberately absent. A public query library must never derive data locations
from its own install path, so callers pass explicit paths instead.
"""

from __future__ import annotations

# Canonical TS parameter codes backing study-level cohort selectors.
STUDY_LENGTH_PARAMETER_CODE = "SLENGTH"
DOSE_DURATION_PARAMETER_CODE = "DOSDUR"
SUPPLIER_PARAMETER_CODE = "SPLRNAM"
STUDY_TYPE_PARAMETER_CODE = "SSTYP"
TEST_FACILITY_PARAMETER_CODE = "TSTFNAM"
GLP_STATUS_PARAMETER_CODE = "GLPFL"
# TS parameter codes of the declared exposure of a study without EX rows.
ROUTE_PARAMETER_CODE = "ROUTE"
DOSE_FREQUENCY_PARAMETER_CODE = "PDOSFRQ"
VEHICLE_PARAMETER_CODE = "TRTV"

# Core identity columns.
STUDY_ID = "STUDYID"
DOMAIN = "DOMAIN"
SUBJECT_ID = "USUBJID"
SUBJECT_ID_SHORT = "SUBJID"
VICT3R_ID = "VICT3RID"
POOL_ID = "POOLID"

# Subject demographic columns.
SPECIES = "SPECIES"
STRAIN = "STRAIN"
SUBSTRAIN = "SBSTRAIN"
SEX = "SEX"
AGE = "AGE"
AGE_TEXT = "AGETXT"
AGE_UNIT = "AGEU"
ARM_CODE = "ARMCD"
ARM = "ARM"
SET_CODE = "SETCD"
ROUTE_PATH_CODE = "RPATHCD"

SUBJECT_SPLIT_COLUMNS = (
    STUDY_ID,
    SUBJECT_ID,
    VICT3R_ID,
    SPECIES,
    STRAIN,
    SUBSTRAIN,
    SEX,
    AGE,
    AGE_TEXT,
    AGE_UNIT,
    ARM_CODE,
    ARM,
    SET_CODE,
    ROUTE_PATH_CODE,
)

# Trial/treatment design columns.
TRIAL_ARM_ORDER = "TAETORD"
TRIAL_ELEMENT_CODE = "ETCD"
TRIAL_ELEMENT = "ELEMENT"
TRIAL_EPOCH = "EPOCH"
TRIAL_BRANCH = "TABRANCH"
TRIAL_TRANSITION = "TATRANS"

TX_SET = "SET"
TX_SEQUENCE = "TXSEQ"
TX_PARAMETER_CODE = "TXPARMCD"
TX_PARAMETER = "TXPARM"
TX_VALUE = "TXVAL"

# Trial summary metadata columns.
TS_SEQUENCE = "TSSEQ"
TS_PARAMETER_CODE = "TSPARMCD"
TS_PARAMETER = "TSPARM"
TS_VALUE = "TSVAL"
TS_GROUP_ID = "TSGRPID"
TS_VALUE_NULL_FLAVOR = "TSVALNF"

# Subject element/timeline columns.
SE_SEQUENCE = "SESEQ"
SE_START = "SESTDTC"
SE_END = "SEENDTC"
SE_UP_DES = "SEUPDES"

# Exposure columns used for canonical route, frequency, treatment, and
# study-day selectors.
EX_SEQUENCE = "EXSEQ"
EX_TREATMENT = "EXTRT"
EX_DOSE = "EXDOSE"
EX_DOSE_TEXT = "EXDOSTXT"
EX_DOSE_UNIT = "EXDOSU"
EX_DOSE_FORM = "EXDOSFRM"
EX_DOSE_FREQUENCY = "EXDOSFRQ"
EX_ROUTE = "EXROUTE"
EX_LOCATION = "EXLOC"
EX_METHOD = "EXMETHOD"
EX_TREATMENT_VALUE = "EXTRTV"
EX_VOLUME = "EXVAMT"
EX_VOLUME_UNIT = "EXVAMTU"
EX_START = "EXSTDTC"
EX_END = "EXENDTC"
EX_START_DAY = "EXSTDY"
EX_END_DAY = "EXENDY"
EX_REL_START_DAY = "EXRPSTDY"
EX_REL_END_DAY = "EXRPENDY"
EX_DURATION = "EXDUR"

TREATMENT_TIMELINE_COLUMNS = (
    STUDY_ID,
    SUBJECT_ID,
    VICT3R_ID,
    TRIAL_ELEMENT_CODE,
    TRIAL_ELEMENT,
    SE_START,
    SE_END,
    EX_TREATMENT,
    EX_DOSE,
    EX_DOSE_TEXT,
    EX_DOSE_UNIT,
    EX_DOSE_FREQUENCY,
    EX_ROUTE,
    EX_METHOD,
    EX_TREATMENT_VALUE,
    EX_START,
    EX_END,
    EX_START_DAY,
    EX_END_DAY,
    EX_REL_START_DAY,
    EX_REL_END_DAY,
    EX_DURATION,
)


RAW_DOMAIN_ORDER = (
    "BG",
    "BW",
    "CL",
    "CO",
    "CV",
    "DD",
    "DM",
    "DS",
    "EG",
    "EX",
    "FW",
    "LB",
    "MA",
    "MI",
    "OM",
    "PC",
    "PM",
    "POOLDEF",
    "PP",
    "RE",
    "RELREC",
    "SC",
    "SE",
    "SUPPCL",
    "SUPPLB",
    "SUPPMA",
    "SUPPMI",
    "SUPPPC",
    "TA",
    "TE",
    "TF",
    "TS",
    "TX",
    "VS",
    "WSI",
    "XV",
)

# Subject-level domains: every raw domain that carries a USUBJID column.
SUBJECT_DOMAINS = tuple(
    domain
    for domain in RAW_DOMAIN_ORDER
    if domain
    not in {
        "POOLDEF",
        "TA",
        "TE",
        "TS",
        "TX",
        "XV",
    }
)

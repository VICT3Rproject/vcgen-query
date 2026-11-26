"""Small shared contract primitives that avoid import cycles."""

import hashlib
from importlib.resources import files


class QueryError(ValueError):
    pass


class SourceDataError(ValueError):
    """Raised when source data cannot satisfy the query identity contract."""


def packaged_data(name: str) -> tuple[bytes, str]:
    """Return the bytes of a data file of this package and their SHA-256."""

    raw = files("vcgen_query").joinpath("data", name).read_bytes()
    return raw, hashlib.sha256(raw).hexdigest()

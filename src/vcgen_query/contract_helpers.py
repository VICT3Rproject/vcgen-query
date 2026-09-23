"""Small shared contract primitives that avoid import cycles."""


class QueryError(ValueError):
    pass


class SourceDataError(ValueError):
    """Raised when source data cannot satisfy the query identity contract."""

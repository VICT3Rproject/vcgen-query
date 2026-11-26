"""Strict readers for the catalogue payloads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def catalogue_mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{location} must be an object")
    return value


def catalogue_sequence(value: Any, location: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise RuntimeError(f"{location} must be an array")
    return value

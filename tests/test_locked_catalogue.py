"""The runtime reads the endpoint catalogue that the caller locks, else the bundled one.

Failure modes:

- the catalogue file is missing, and the loader uses another catalogue;
- one byte of the catalogue changed, and the loader accepts it;
- the lock is missing or names no catalogue, and the loader accepts any file;
- the caller names only the catalogue or only the lock, and the loader silently
  uses the bundled catalogue and no data decisions;
- the caller names a locked catalogue, and the loader uses the bundled one.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from vcgen_query.contract_helpers import QueryError
from vcgen_query.data_decisions import EMPTY_DATA_DECISIONS, load_data_decisions
from vcgen_query.endpoint_catalogue import (
    bundled_catalogue_bytes,
    catalogue_location,
    load_endpoint_catalogue,
    load_locked_catalogue,
)

# A valid catalogue to lock; the content does not matter to the loader.
CATALOGUE = bundled_catalogue_bytes()


def _locked(root: Path, raw: bytes = CATALOGUE) -> tuple[Path, Path]:
    catalogue = root / "catalogue.json"
    lock = root / "lock.yaml"
    catalogue.write_bytes(raw)
    lock.write_text(yaml.safe_dump({"contract": "vcgen.catalogue_lock.v1",
                                    "outputs": {"catalogue.json": hashlib.md5(CATALOGUE).hexdigest()}}))
    return catalogue, lock


@pytest.fixture
def active_catalogue(monkeypatch):
    """Start with no caller catalogue, and clear the per-process catalogue and decisions around the test."""

    monkeypatch.delenv("VCGEN_ENDPOINT_CATALOGUE", raising=False)
    monkeypatch.delenv("VCGEN_ENDPOINT_CATALOGUE_LOCK", raising=False)
    load_endpoint_catalogue.cache_clear()
    load_data_decisions.cache_clear()
    yield
    load_endpoint_catalogue.cache_clear()
    load_data_decisions.cache_clear()


def test_the_locked_catalogue_loads(tmp_path):
    catalogue, lock = _locked(tmp_path)
    assert load_locked_catalogue(catalogue, lock).identity.md5 == hashlib.md5(CATALOGUE).hexdigest()


def test_a_changed_byte_stops_the_loader(tmp_path):
    catalogue, lock = _locked(tmp_path, CATALOGUE + b" ")
    with pytest.raises(QueryError, match="differs from the lock"):
        load_locked_catalogue(catalogue, lock)


def test_a_missing_catalogue_or_lock_stops_the_loader(tmp_path):
    catalogue, lock = _locked(tmp_path)
    with pytest.raises(QueryError, match="is missing"):
        load_locked_catalogue(catalogue.with_name("missing.json"), lock)
    with pytest.raises(QueryError, match="lock"):
        load_locked_catalogue(catalogue, lock.with_name("missing.yaml"))
    lock.write_text(yaml.safe_dump({"contract": "vcgen.catalogue_lock.v1", "outputs": {}}))
    with pytest.raises(QueryError, match="lock"):
        load_locked_catalogue(catalogue, lock)


@pytest.mark.parametrize("name", ["VCGEN_ENDPOINT_CATALOGUE", "VCGEN_ENDPOINT_CATALOGUE_LOCK"])
def test_a_half_named_catalogue_is_refused(tmp_path, monkeypatch, active_catalogue, name):
    monkeypatch.setenv(name, str(tmp_path / "named"))
    with pytest.raises(QueryError, match="Set both"):
        load_endpoint_catalogue()
    with pytest.raises(QueryError, match="Set both"):
        load_data_decisions()


def test_the_caller_locked_catalogue_replaces_the_bundled_one(tmp_path, monkeypatch, active_catalogue):
    catalogue, lock = _locked(tmp_path, CATALOGUE + b"\n")
    lock.write_text(yaml.safe_dump({"outputs": {"catalogue.json": hashlib.md5(CATALOGUE + b"\n").hexdigest()}}))
    monkeypatch.setenv("VCGEN_ENDPOINT_CATALOGUE", str(catalogue))
    monkeypatch.setenv("VCGEN_ENDPOINT_CATALOGUE_LOCK", str(lock))
    assert catalogue_location() == (catalogue, lock)
    assert load_endpoint_catalogue().identity.md5 == hashlib.md5(CATALOGUE + b"\n").hexdigest()
    with pytest.raises(QueryError, match="Data decisions .* are missing"):
        load_data_decisions()


def test_without_a_caller_catalogue_the_bundled_one_has_no_data_decisions(active_catalogue):
    assert catalogue_location() is None
    assert load_endpoint_catalogue().identity.md5 == hashlib.md5(CATALOGUE).hexdigest()
    assert load_data_decisions() is EMPTY_DATA_DECISIONS

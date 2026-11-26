"""The runtime reads only the locked endpoint catalogue (C6, src/vcgen_ai/curation/catalogue/README.md).

Failure modes:

- the catalogue file is missing, and the loader uses another catalogue;
- one byte of the catalogue changed, and the loader accepts it;
- the lock is missing or names no catalogue, and the loader accepts any file;
- `VCGEN_ENDPOINT_CATALOGUE` is ignored, so a test or a service reads the
  catalogue of the source root.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from vcgen_query.contract_helpers import QueryError
from vcgen_query.endpoint_catalogue import catalogue_location, load_locked_catalogue

# A valid catalogue to lock (the locked catalogue of the source root); the content does not matter to the loader.
CATALOGUE = catalogue_location()[0].read_bytes()


def _locked(root: Path, raw: bytes = CATALOGUE) -> tuple[Path, Path]:
    catalogue = root / "data/catalogue/catalogue.json"
    lock = root / "config/curation/catalogue/lock.yaml"
    catalogue.parent.mkdir(parents=True)
    lock.parent.mkdir(parents=True)
    catalogue.write_bytes(raw)
    lock.write_text(yaml.safe_dump({"contract": "vcgen.catalogue_lock.v1",
                                    "outputs": {"catalogue.json": hashlib.md5(CATALOGUE).hexdigest()}}))
    return catalogue, lock


def test_the_locked_catalogue_loads(tmp_path):
    catalogue, lock = _locked(tmp_path)
    assert load_locked_catalogue(catalogue, lock).identity.md5 == hashlib.md5(CATALOGUE).hexdigest()


def test_a_changed_byte_stops_the_loader(tmp_path):
    catalogue, lock = _locked(tmp_path, CATALOGUE + b" ")
    with pytest.raises(QueryError, match="differs from the lock"):
        load_locked_catalogue(catalogue, lock)


def test_a_missing_catalogue_or_lock_stops_the_loader(tmp_path):
    catalogue, lock = _locked(tmp_path)
    with pytest.raises(QueryError, match="dvc pull"):
        load_locked_catalogue(catalogue.with_name("missing.json"), lock)
    with pytest.raises(QueryError, match="lock"):
        load_locked_catalogue(catalogue, lock.with_name("missing.yaml"))
    lock.write_text(yaml.safe_dump({"contract": "vcgen.catalogue_lock.v1", "outputs": {}}))
    with pytest.raises(QueryError, match="lock"):
        load_locked_catalogue(catalogue, lock)


def test_the_location_follows_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("VCGEN_SOURCE_ROOT", str(tmp_path))
    monkeypatch.delenv("VCGEN_ENDPOINT_CATALOGUE", raising=False)
    monkeypatch.delenv("VCGEN_ENDPOINT_CATALOGUE_LOCK", raising=False)
    assert catalogue_location() == (tmp_path / "data/catalogue/catalogue.json",
                                    tmp_path / "config/curation/catalogue/lock.yaml")
    monkeypatch.setenv("VCGEN_ENDPOINT_CATALOGUE", str(tmp_path / "a.json"))
    monkeypatch.setenv("VCGEN_ENDPOINT_CATALOGUE_LOCK", str(tmp_path / "a.yaml"))
    assert catalogue_location() == (tmp_path / "a.json", tmp_path / "a.yaml")

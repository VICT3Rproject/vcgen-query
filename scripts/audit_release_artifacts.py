#!/usr/bin/env python3
"""Reject unexpected paths in built vcgen-query release archives."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


_FORBIDDEN_PARTS = {
    ".cache",
    ".pytest_cache",
    ".vcgen_query_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}
_SDIST_ALLOWED_ROOTS = {
    ".gitignore",
    ".vscode",
    "GLOSSARY.md",
    "GRIT42_SAVED_QUERY_GUIDE.md",
    "notebooks",
    "PKG-INFO",
    "pyproject.toml",
    "README.md",
    "src",
}


def _member_names(artifact: Path) -> list[str]:
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            return archive.namelist()
    if artifact.name.endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
        with tarfile.open(artifact, "r:*") as archive:
            return [member.name for member in archive.getmembers()]
    raise ValueError(f"unsupported release archive: {artifact}")


def _safe_parts(name: str) -> tuple[str, ...]:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe archive path: {name}")
    return tuple(part for part in path.parts if part not in {"", "."})


def _audit_wheel(artifact: Path, names: list[str]) -> None:
    for name in names:
        parts = _safe_parts(name)
        if not parts:
            continue
        root = parts[0]
        allowed_root = root == "vcgen_query" or (
            root.startswith("vcgen_query-") and root.endswith(".dist-info")
        )
        if not allowed_root:
            raise ValueError(f"unexpected wheel path: {name}")
        if _FORBIDDEN_PARTS.intersection(parts):
            raise ValueError(f"forbidden wheel path: {name}")


def _audit_sdist(artifact: Path, names: list[str]) -> None:
    parts_by_name = [(name, _safe_parts(name)) for name in names]
    roots = {parts[0] for _, parts in parts_by_name if parts}
    if len(roots) != 1:
        raise ValueError(f"source distribution must have one root: {sorted(roots)}")
    archive_root = next(iter(roots))
    if not archive_root.startswith("vcgen_query-"):
        raise ValueError(f"unexpected source-distribution root: {archive_root}")

    for name, parts in parts_by_name:
        relative = parts[1:]
        if not relative:
            continue
        if _FORBIDDEN_PARTS.intersection(relative):
            raise ValueError(f"forbidden source-distribution path: {name}")
        if relative[0] not in _SDIST_ALLOWED_ROOTS:
            raise ValueError(f"unexpected source-distribution path: {name}")


def audit_artifact(artifact: Path) -> None:
    """Raise ValueError when an artifact contains an unexpected path."""

    names = _member_names(artifact)
    if artifact.suffix == ".whl":
        _audit_wheel(artifact, names)
    else:
        _audit_sdist(artifact, names)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", type=Path)
    arguments = parser.parse_args()

    for artifact in arguments.artifacts:
        audit_artifact(artifact)
        print(f"release artifact is clean: {artifact}")


if __name__ == "__main__":
    main()

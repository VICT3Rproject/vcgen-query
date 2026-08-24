"""Release archives contain only intended package and documentation files."""

from __future__ import annotations

import io
import runpy
import tarfile
import zipfile
from pathlib import Path

import pytest


_AUDIT_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_release_artifacts.py"
audit_artifact = runpy.run_path(str(_AUDIT_SCRIPT))["audit_artifact"]


def test_release_artifact_audit_accepts_expected_wheel_layout(tmp_path) -> None:
    wheel = tmp_path / "vcgen_query-0.3.0a0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("vcgen_query/__init__.py", "")
        archive.writestr("vcgen_query-0.3.0a0.dist-info/METADATA", "")

    audit_artifact(wheel)


def test_release_artifact_audit_rejects_nested_uv_cache(tmp_path) -> None:
    sdist = tmp_path / "vcgen_query-0.3.0a0.tar.gz"
    member_name = "vcgen_query-0.3.0a0/.cache/uv/archive/README.md"
    content = b"unexpected cache file"
    member = tarfile.TarInfo(member_name)
    member.size = len(content)
    with tarfile.open(sdist, "w:gz") as archive:
        archive.addfile(member, io.BytesIO(content))

    with pytest.raises(ValueError, match="forbidden source-distribution path"):
        audit_artifact(sdist)

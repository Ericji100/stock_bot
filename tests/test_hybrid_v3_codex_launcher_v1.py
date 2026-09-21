from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import hybrid_v3_codex_launcher_v1 as launcher
from scripts.hybrid_v3_atomic_runner_v2 import ReviewContractError


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_verify_launcher_pin_accepts_exact_file(tmp_path: Path) -> None:
    contract = {
        "v2_components": [
            {
                "name": "launcher",
                "relative_path": "scripts/hybrid_v3_codex_launcher_v1.py",
                "status": "FINAL",
                "sha256": _sha(Path(launcher.__file__).resolve()),
            }
        ]
    }
    path = tmp_path / "freeze.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    assert launcher._verify_launcher_pin(path) == contract["v2_components"][0]["sha256"]


def test_verify_launcher_pin_fails_closed_on_hash_mismatch(tmp_path: Path) -> None:
    contract = {
        "v2_components": [
            {
                "name": "launcher",
                "relative_path": "scripts/hybrid_v3_codex_launcher_v1.py",
                "status": "FINAL",
                "sha256": "0" * 64,
            }
        ]
    }
    path = tmp_path / "freeze.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(ReviewContractError, match="launcher hash differs"):
        launcher._verify_launcher_pin(path)

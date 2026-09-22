"""Canonical local work-data paths shared by production and development workspaces."""

from __future__ import annotations

import os
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKDATA_ROOT_ENV = "STOCK_AI_BOT_WORKDATA_ROOT"
WORKDATA_PROFILE_ENV = "STOCK_AI_BOT_WORKDATA_PROFILE"
DEFAULT_PROFILE = "prod"
_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def get_workdata_root(project_root: Path | None = None) -> Path:
    """Return the local, Git-ignored root that owns mutable workspace data."""

    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    configured = os.environ.get(WORKDATA_ROOT_ENV, "").strip()
    candidate = Path(configured).expanduser() if configured else root / ".workdata"
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve(strict=False)


def get_workdata_profile() -> str:
    """Return a validated profile name such as ``prod`` or ``tmf-monitor``."""

    profile = os.environ.get(WORKDATA_PROFILE_ENV, DEFAULT_PROFILE).strip()
    if not _PROFILE_PATTERN.fullmatch(profile):
        raise ValueError(
            f"Invalid {WORKDATA_PROFILE_ENV}: {profile!r}. "
            "Use letters, digits, dot, underscore, or hyphen."
        )
    return profile


def get_workspace_data_root(
    project_root: Path | None = None,
    *,
    profile: str | None = None,
) -> Path:
    """Return the isolated data root for production or one development profile."""

    selected = profile if profile is not None else get_workdata_profile()
    if not _PROFILE_PATTERN.fullmatch(selected):
        raise ValueError(f"Invalid work-data profile: {selected!r}")
    root = get_workdata_root(project_root)
    return root / "prod" if selected == DEFAULT_PROFILE else root / "dev" / selected


def get_workdata_path(
    *parts: str | os.PathLike[str],
    project_root: Path | None = None,
    profile: str | None = None,
) -> Path:
    """Resolve a relative path below the selected profile without allowing escapes."""

    relative = Path(*parts)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Work-data path must stay relative to its profile: {relative}")
    return get_workspace_data_root(project_root, profile=profile) / relative

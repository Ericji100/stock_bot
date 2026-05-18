"""Shared test utilities for cache isolation.

Provides workspace-based test cache directories under .cache/test_tmp/
to avoid Windows PermissionError issues with system Temp directories.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Root for all test cache files
_TEST_CACHE_ROOT = Path(".cache") / "test_tmp"


def ensure_test_cache_dir(sub_path: str) -> Path:
    """Create and return a workspace test cache directory.

    Always cleans up any existing directory at the same path first,
    so each test gets a completely fresh directory.
    """
    full_path = _TEST_CACHE_ROOT / sub_path
    # Safety: only allow deletion within .cache/test_tmp/
    resolved_root = _TEST_CACHE_ROOT.resolve()
    if not full_path.resolve().as_posix().startswith(resolved_root.as_posix()):
        raise ValueError(f"Refusing to create path outside test cache root: {full_path}")

    # Remove existing tree safely
    if full_path.exists():
        shutil.rmtree(full_path, ignore_errors=True)
        # On Windows, sometimes files remain locked briefly
        try:
            if full_path.exists():
                os.chmod(full_path, 0o755)
                shutil.rmtree(full_path, ignore_errors=True)
        except Exception:
            pass

    full_path.mkdir(parents=True, exist_ok=True)
    return full_path


def safe_remove_test_cache(sub_path: str) -> None:
    """Safely remove a test cache subdirectory.

    Only removes paths under .cache/test_tmp/.
    Silently ignores failures.
    """
    full_path = _TEST_CACHE_ROOT / sub_path
    resolved_root = _TEST_CACHE_ROOT.resolve()
    if not full_path.resolve().as_posix().startswith(resolved_root.as_posix()):
        return  # Safety: refuse to delete outside test root

    try:
        if full_path.exists():
            shutil.rmtree(full_path, ignore_errors=True)
    except Exception:
        pass
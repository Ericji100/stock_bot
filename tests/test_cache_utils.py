"""Shared test utilities for cache isolation.

Provides workspace-based test cache directories under .cache/test_tmp/
to avoid Windows PermissionError issues with system Temp directories.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

# Root for all test cache files
_TEST_CACHE_ROOT = Path(".cache") / "test_tmp"


def _make_tree_writable(path: Path) -> None:
    """Best-effort chmod for a test cache tree on Windows."""
    try:
        os.chmod(path, 0o755)
    except Exception:
        pass
    if not path.exists():
        return
    for item in path.rglob("*"):
        try:
            os.chmod(item, 0o755)
        except Exception:
            pass


def _remove_tree_with_retries(path: Path, *, retries: int = 5, delay: float = 0.2) -> bool:
    """Remove a test cache tree, returning True only when it is gone."""
    for _ in range(retries):
        if not path.exists():
            return True
        _make_tree_writable(path)
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        time.sleep(delay)
    return not path.exists()


def ensure_test_cache_dir(sub_path: str) -> Path:
    """Create and return a workspace test cache directory.

    Always cleans up any existing directory at the same path first,
    so each test gets a completely fresh directory.

    If an existing directory cannot be removed because Windows still holds a
    transient lock, this function returns a unique clean sibling directory
    instead of reusing dirty data.

    Raises:
        ValueError: If the path is outside the test cache root.
    """
    full_path = _TEST_CACHE_ROOT / sub_path
    # Safety: only allow deletion within .cache/test_tmp/
    resolved_root = _TEST_CACHE_ROOT.resolve()
    if not full_path.resolve().as_posix().startswith(resolved_root.as_posix()):
        raise ValueError(f"Refusing to create path outside test cache root: {full_path}")

    # Remove existing tree with retries
    if full_path.exists():
        if not _remove_tree_with_retries(full_path):
            full_path = full_path.parent / f"{full_path.name}_{os.getpid()}_{time.time_ns()}"
            if not full_path.resolve().as_posix().startswith(resolved_root.as_posix()):
                raise ValueError(f"Refusing to create fallback path outside test cache root: {full_path}")

    # Create fresh directory
    full_path.mkdir(parents=True, exist_ok=False)
    return full_path


def safe_remove_test_cache(sub_path: str) -> None:
    """Safely remove a test cache subdirectory.

    Only removes paths under .cache/test_tmp/.
    Silently ignores failures (unlike ensure_test_cache_dir which raises).
    """
    full_path = _TEST_CACHE_ROOT / sub_path
    resolved_root = _TEST_CACHE_ROOT.resolve()
    if not full_path.resolve().as_posix().startswith(resolved_root.as_posix()):
        return  # Safety: refuse to delete outside test root

    _remove_tree_with_retries(full_path)

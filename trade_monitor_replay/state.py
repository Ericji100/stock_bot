from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_RUNTIME_ROOT = Path(".runtime/trade_monitor_replay")


class ReplayStateError(RuntimeError):
    pass


def create_run_directory(
    *,
    target_date: str,
    instrument: str,
    rule_version: str,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> Path:
    now = datetime.now(TAIPEI)
    # Keep the logical rule version in manifest.json, but do not place the full
    # version string in every Windows artifact path.  Long version labels plus
    # atomic-write temp suffixes can otherwise exceed the legacy MAX_PATH limit.
    rule_tag = hashlib.sha256(rule_version.encode("utf-8")).hexdigest()[:8]
    run_id = (
        f"replay-{target_date.replace('-', '')}-{instrument}-r{rule_tag}-"
        f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
    )
    run_dir = (runtime_root / "runs" / run_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def resolve_run_directory(run_id: str, *, runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> Path:
    text = str(run_id or "").strip()
    if not text or Path(text).name != text:
        raise ReplayStateError("run_id格式無效。")
    base = (runtime_root / "runs").resolve()
    path = (base / text).resolve()
    if base not in path.parents or not path.is_dir():
        raise ReplayStateError("找不到指定的回放run。")
    return path


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayStateError(f"無法讀取回放狀態：{path.name}") from exc


def write_json_atomic(path: Path, payload: Mapping[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ReplayStateError(f"無法保存回放狀態：{path.name}") from exc


def append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise ReplayStateError(f"無法追加回放紀錄：{path.name}") from exc

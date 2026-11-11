"""Separate authorization so the approved monitor configuration stays fixed."""
import json
from dataclasses import dataclass
from pathlib import Path


CONFIG_PATH = Path(__file__).resolve().parents[3] / "config/etf_position_sizing.json"


@dataclass(frozen=True)
class ReplyConfig:
    enabled: bool
    allowed_user_ids: frozenset[int]


def load_reply_config(path: Path = CONFIG_PATH) -> ReplyConfig:
    if not path.is_file():
        return ReplyConfig(False, frozenset())
    raw = json.loads(path.read_text(encoding="utf-8"))
    if set(raw) != {"enabled", "allowed_user_ids"} or not isinstance(raw["enabled"], bool):
        raise ValueError("invalid 0050 sizing configuration")
    ids = raw["allowed_user_ids"]
    if not isinstance(ids, list) or any(type(value) is not int or value <= 0 for value in ids):
        raise ValueError("invalid 0050 sizing user list")
    if raw["enabled"] and not ids:
        raise ValueError("0050 sizing requires explicit users")
    return ReplyConfig(raw["enabled"], frozenset(ids))

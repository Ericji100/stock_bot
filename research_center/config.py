from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "gemini-3-pro-preview"
DEFAULT_FALLBACK_MODELS = ("gemini-3-flash-preview",)
DEFAULT_MINIMAX_MODEL = "MiniMax-M2.7"
DEFAULT_MINIMAX_BASE_URL = "https://api.minimax.io/v1"


@dataclass(frozen=True)
class ResearchCenterConfig:
    model: str = DEFAULT_MODEL
    fallback_models: tuple[str, ...] = DEFAULT_FALLBACK_MODELS
    enable_grounding: bool = True
    api_key: str | None = None
    minimax_api_key: str | None = None
    minimax_model: str = DEFAULT_MINIMAX_MODEL
    minimax_base_url: str = DEFAULT_MINIMAX_BASE_URL
    serper_api_key: str | None = None
    jina_api_key: str | None = None
    enable_minimax_search: bool = False
    enable_minimax_comparison: bool = False
    api_token: str | None = None
    report_root: Path = ROOT_DIR / "reports"
    database_path: Path = ROOT_DIR / "database" / "stock_research.db"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    output_formats: tuple[str, ...] = ("md", "html", "json")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def load_research_config(root_dir: Path | None = None) -> ResearchCenterConfig:
    root = root_dir or ROOT_DIR
    public_config = _read_json(root / "config" / "research_center.json")
    secrets = _read_json(root / "config" / "secrets.json")

    model = str(public_config.get("model") or secrets.get("gemini_model") or DEFAULT_MODEL)
    fallback_models = tuple(
        str(item).strip()
        for item in (public_config.get("fallback_models") or secrets.get("gemini_fallback_models") or DEFAULT_FALLBACK_MODELS)
        if str(item).strip() and str(item).strip() != model
    )
    enable_grounding = bool(public_config.get("enable_grounding", True))
    report_root = root / str(public_config.get("report_root", "reports"))
    database_path = root / str(public_config.get("database_path", "database/stock_research.db"))
    api_host = str(public_config.get("api_host", "127.0.0.1"))
    api_port = int(public_config.get("api_port", 8000))
    formats = tuple(public_config.get("output_formats", ["md", "html", "json"]))
    api_key = secrets.get("gemini_api_key") or secrets.get("google_api_key")
    minimax_api_key = secrets.get("minimax_api_key")
    minimax_model = str(public_config.get("minimax_model") or secrets.get("minimax_model") or DEFAULT_MINIMAX_MODEL)
    minimax_base_url = str(public_config.get("minimax_base_url") or secrets.get("minimax_base_url") or DEFAULT_MINIMAX_BASE_URL)
    serper_api_key = secrets.get("serper_api_key")
    jina_api_key = secrets.get("jina_api_key")
    enable_minimax_search = bool(public_config.get("enable_minimax_search", bool(serper_api_key)))
    enable_minimax_comparison = bool(public_config.get("enable_minimax_comparison", bool(minimax_api_key)))
    api_token = secrets.get("research_api_token") or public_config.get("api_token")

    return ResearchCenterConfig(
        model=model,
        fallback_models=fallback_models,
        enable_grounding=enable_grounding,
        api_key=str(api_key).strip() if api_key else None,
        minimax_api_key=str(minimax_api_key).strip() if minimax_api_key else None,
        minimax_model=minimax_model,
        minimax_base_url=minimax_base_url,
        serper_api_key=str(serper_api_key).strip() if serper_api_key else None,
        jina_api_key=str(jina_api_key).strip() if jina_api_key else None,
        enable_minimax_search=enable_minimax_search,
        enable_minimax_comparison=enable_minimax_comparison,
        api_token=str(api_token).strip() if api_token else None,
        report_root=report_root,
        database_path=database_path,
        api_host=api_host,
        api_port=api_port,
        output_formats=formats,
    )
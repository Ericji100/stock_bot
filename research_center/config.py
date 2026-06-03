from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "gemini-3-pro-preview"
DEFAULT_FALLBACK_MODELS = ("gemini-3-flash-preview",)
DEFAULT_MINIMAX_MODEL = "MiniMax-M3"
DEFAULT_MINIMAX_LOW_MODEL = "MiniMax-M2.7"
DEFAULT_MINIMAX_BASE_URL = "https://api.minimax.io/v1"
DEFAULT_OPENCODE_MODEL = "deepseek-v4-pro"
DEFAULT_OPENCODE_BASE_URL = "https://opencode.ai/zen/go/v1"
DEFAULT_OPENCODE_REASONING_EFFORT = "medium"


@dataclass(frozen=True)
class ResearchCenterConfig:
    model: str = DEFAULT_MODEL
    fallback_models: tuple[str, ...] = DEFAULT_FALLBACK_MODELS
    enable_grounding: bool = True
    api_key: str | None = None
    minimax_api_key: str | None = None
    minimax_model: str = DEFAULT_MINIMAX_MODEL
    minimax_low_model: str = DEFAULT_MINIMAX_LOW_MODEL
    minimax_base_url: str = DEFAULT_MINIMAX_BASE_URL
    enable_low_model_digest: bool = True
    opencode_api_key: str | None = None
    opencode_model: str = DEFAULT_OPENCODE_MODEL
    opencode_base_url: str = DEFAULT_OPENCODE_BASE_URL
    opencode_reasoning_effort: str = DEFAULT_OPENCODE_REASONING_EFFORT
    enable_opencode_analysis: bool = False
    serper_api_key: str | None = None
    jina_api_key: str | None = None
    enable_minimax_search: bool = False
    enable_minimax_comparison: bool = False
    minimax_mcp_timeout_seconds: float = 60.0
    minimax_mcp_max_results_per_query: int = 10
    enable_serper_search: bool = False
    enable_jina_reader: bool = False
    enable_tavily_search: bool = True
    enable_tavily_extract: bool = True
    gemini_search_mode: str = "fallback"
    tavily_api_key: str | None = None
    tavily_api_keys: tuple[str, ...] = ()
    tavily_monthly_credit_limit: int = 1000
    tavily_credit_reserve: int = 20
    tavily_search_depth: str = "basic"
    tavily_extract_depth: str = "basic"
    tavily_max_results_per_query: int = 5
    tavily_max_extract_urls_per_task: int = 5
    gemini_fallback_thresholds: dict[str, dict[str, int]] = field(default_factory=dict)
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
    minimax_low_model = str(public_config.get("minimax_low_model") or secrets.get("minimax_low_model") or DEFAULT_MINIMAX_LOW_MODEL)
    minimax_base_url = str(public_config.get("minimax_base_url") or secrets.get("minimax_base_url") or DEFAULT_MINIMAX_BASE_URL)
    enable_low_model_digest = bool(public_config.get("enable_low_model_digest", bool(minimax_api_key)))
    opencode_api_key = secrets.get("opencode_api_key")
    opencode_model = str(public_config.get("opencode_model") or secrets.get("opencode_model") or DEFAULT_OPENCODE_MODEL)
    opencode_base_url = str(public_config.get("opencode_base_url") or secrets.get("opencode_base_url") or DEFAULT_OPENCODE_BASE_URL)
    opencode_reasoning_effort = str(public_config.get("opencode_reasoning_effort") or secrets.get("opencode_reasoning_effort") or DEFAULT_OPENCODE_REASONING_EFFORT)
    enable_opencode_analysis = bool(public_config.get("enable_opencode_analysis", bool(opencode_api_key)))
    serper_api_key = secrets.get("serper_api_key")
    jina_api_key = secrets.get("jina_api_key")
    enable_minimax_search = bool(public_config.get("enable_minimax_search", False))
    enable_minimax_comparison = bool(public_config.get("enable_minimax_comparison", bool(minimax_api_key)))
    minimax_mcp_timeout_seconds = float(public_config.get("minimax_mcp_timeout_seconds", 60.0))
    minimax_mcp_max_results_per_query = int(public_config.get("minimax_mcp_max_results_per_query", 10))
    enable_serper_search = bool(public_config.get("enable_serper_search", False))
    enable_jina_reader = bool(public_config.get("enable_jina_reader", False))
    enable_tavily_search = bool(public_config.get("enable_tavily_search", True))
    enable_tavily_extract = bool(public_config.get("enable_tavily_extract", True))
    gemini_search_mode = str(public_config.get("gemini_search_mode", "fallback"))
    tavily_api_key = secrets.get("tavily_api_key")
    tavily_api_keys = _normalize_api_keys(secrets.get("tavily_api_keys"), tavily_api_key)
    tavily_monthly_credit_limit = int(public_config.get("tavily_monthly_credit_limit", 1000))
    tavily_credit_reserve = int(public_config.get("tavily_credit_reserve", 20))
    tavily_search_depth = str(public_config.get("tavily_search_depth", "basic"))
    tavily_extract_depth = str(public_config.get("tavily_extract_depth", "basic"))
    tavily_max_results_per_query = int(public_config.get("tavily_max_results_per_query", 5))
    tavily_max_extract_urls_per_task = int(public_config.get("tavily_max_extract_urls_per_task", 5))
    gemini_fallback_thresholds = public_config.get("gemini_fallback_thresholds") or {}
    api_token = secrets.get("research_api_token") or public_config.get("api_token")

    return ResearchCenterConfig(
        model=model,
        fallback_models=fallback_models,
        enable_grounding=enable_grounding,
        api_key=str(api_key).strip() if api_key else None,
        minimax_api_key=str(minimax_api_key).strip() if minimax_api_key else None,
        minimax_model=minimax_model,
        minimax_low_model=minimax_low_model,
        minimax_base_url=minimax_base_url,
        enable_low_model_digest=enable_low_model_digest,
        opencode_api_key=str(opencode_api_key).strip() if opencode_api_key else None,
        opencode_model=opencode_model,
        opencode_base_url=opencode_base_url,
        opencode_reasoning_effort=opencode_reasoning_effort,
        enable_opencode_analysis=enable_opencode_analysis,
        serper_api_key=str(serper_api_key).strip() if serper_api_key else None,
        jina_api_key=str(jina_api_key).strip() if jina_api_key else None,
        enable_minimax_search=enable_minimax_search,
        enable_minimax_comparison=enable_minimax_comparison,
        enable_serper_search=enable_serper_search,
        enable_jina_reader=enable_jina_reader,
        enable_tavily_search=enable_tavily_search,
        enable_tavily_extract=enable_tavily_extract,
        gemini_search_mode=gemini_search_mode,
        tavily_api_key=str(tavily_api_key).strip() if tavily_api_key else None,
        tavily_api_keys=tavily_api_keys,
        tavily_monthly_credit_limit=tavily_monthly_credit_limit,
        tavily_credit_reserve=tavily_credit_reserve,
        tavily_search_depth=tavily_search_depth,
        tavily_extract_depth=tavily_extract_depth,
        tavily_max_results_per_query=tavily_max_results_per_query,
        tavily_max_extract_urls_per_task=tavily_max_extract_urls_per_task,
        gemini_fallback_thresholds=gemini_fallback_thresholds,
        api_token=str(api_token).strip() if api_token else None,
        report_root=report_root,
        database_path=database_path,
        api_host=api_host,
        api_port=api_port,
        output_formats=formats,
    )


def _normalize_api_keys(value: Any, fallback: Any = None) -> tuple[str, ...]:
    raw_items: list[Any]
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        raw_items = [part.strip() for part in value.replace("\n", ",").split(",")]
    else:
        raw_items = []
    if fallback:
        raw_items.insert(0, fallback)
    keys: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        key = str(item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return tuple(keys)

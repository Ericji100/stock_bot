from __future__ import annotations

from datetime import date
from typing import Any

try:
    from fastapi import Depends, FastAPI, Header, HTTPException
    from pydantic import BaseModel
except ImportError:  # pragma: no cover
    Depends = None  # type: ignore
    FastAPI = None  # type: ignore
    Header = None  # type: ignore
    HTTPException = Exception  # type: ignore
    BaseModel = object  # type: ignore

from .config import load_research_config
from .data_services import collect_research_data
from .models import CommandRequest
from .orchestrator import ResearchCenter

center = ResearchCenter(load_research_config())


if FastAPI is not None:
    app = FastAPI(title="AI Investment Research Center")
else:  # pragma: no cover
    app = None


class ResearchPayload(BaseModel):
    stock_id: str
    mode: str = "normal"
    report_date: str | None = None
    source_only: bool = False


class MacroPayload(BaseModel):
    market_scope: str = "??"
    theme_scope: str | None = None
    mode: str = "normal"
    report_date: str | None = None


class ThemePayload(BaseModel):
    theme: str
    mode: str = "normal"
    top: int | None = None
    report_date: str | None = None


class ValueScanPayload(BaseModel):
    candidate_pool: str = "精選選股"
    mode: str = "normal"
    top: int | None = None
    report_date: str | None = None


if Header is not None:
    def require_api_token(
        authorization: str | None = Header(default=None),
        x_research_token: str | None = Header(default=None),
    ) -> None:
        expected = center.config.api_token
        if not expected:
            raise HTTPException(status_code=503, detail="research api token is not configured")
        token = _extract_bearer_token(authorization) or x_research_token
        if token != expected:
            raise HTTPException(status_code=401, detail="invalid or missing research api token")
else:  # pragma: no cover
    def require_api_token() -> None:
        return None


if app is not None:
    @app.post("/research", dependencies=[Depends(require_api_token)])
    def post_research(payload: ResearchPayload) -> dict[str, Any]:
        flags = _mode_flags(payload.mode, payload.source_only)
        return _run_text(f"/research {payload.stock_id}{flags}{_date_flag(payload.report_date)}")

    @app.post("/macro", dependencies=[Depends(require_api_token)])
    def post_macro(payload: MacroPayload) -> dict[str, Any]:
        scope = " ".join(part for part in [payload.market_scope, payload.theme_scope] if part)
        return _run_text(f"/macro {scope}{_mode_flags(payload.mode)}{_date_flag(payload.report_date)}")

    @app.post("/theme", dependencies=[Depends(require_api_token)])
    def post_theme(payload: ThemePayload) -> dict[str, Any]:
        top = f" --top {payload.top}" if payload.top else ""
        return _run_text(f"/theme {payload.theme}{_mode_flags(payload.mode)}{top}{_date_flag(payload.report_date)}")

    @app.post("/value_scan", dependencies=[Depends(require_api_token)])
    def post_value_scan(payload: ValueScanPayload) -> dict[str, Any]:
        top = f" --top {payload.top}" if payload.top else ""
        return _run_text(f"/value_scan {payload.candidate_pool}{_mode_flags(payload.mode)}{top}{_date_flag(payload.report_date)}")

    @app.get("/reports/{report_id}", dependencies=[Depends(require_api_token)])
    def get_report(report_id: str) -> dict[str, Any]:
        row = center.database.get_report(report_id)
        if not row:
            raise HTTPException(status_code=404, detail="report not found")
        return row

    @app.get("/stock/{stock_id}/data", dependencies=[Depends(require_api_token)])
    def get_stock_data(stock_id: str) -> dict[str, Any]:
        request = CommandRequest(command="research", raw_text=f"/research {stock_id} --source-only", target=stock_id, target_type="stock", source_only=True, mode="source_only")
        return collect_research_data(request)


def _extract_bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    prefix = "Bearer "
    if value.startswith(prefix):
        return value[len(prefix):].strip()
    return None


def _run_text(text: str) -> dict[str, Any]:
    try:
        result = center.run_text_command(text)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": result.status,
        "summary": result.summary,
        "report_id": result.artifacts.report_id,
        "markdown_path": str(result.artifacts.markdown_path),
        "html_path": str(result.artifacts.html_path),
        "json_path": str(result.artifacts.json_path),
        "ai_used": result.ai_used,
        "fallback_reason": result.fallback_reason,
    }


def _mode_flags(mode: str, source_only: bool = False) -> str:
    if source_only or mode == "source_only":
        return " --source-only"
    if mode == "deep":
        return " --deep"
    if mode == "score":
        return " --score"
    if mode == "brief":
        return " --brief"
    return ""


def _date_flag(value: str | None) -> str:
    return f" --date {value}" if value else ""

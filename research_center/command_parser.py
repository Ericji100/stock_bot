from __future__ import annotations

import re
import shlex
from datetime import date, datetime

from .models import CommandParseError, CommandRequest

SUPPORTED_COMMANDS = {
    "research",
    "macro",
    "theme",
    "theme_radar",
    "theme_flow",
    "sector_strength",
    "value_scan",
    "report",
    "topic_maintain",
    "topic_review",
    "topic_confirm",
    "topic_reject",
    "topic_profiles",
    "topic_reset",
    "topic_seed_prompt",
    "topic_import",
    "topic_source_sync",
    "news",
    "news_detail",
    "data_status",
    "backfill_status",
    "news_status",
}
REGION_MAP = {
    "台股": "台灣",
    "台灣": "台灣",
    "全球": "global",
    "美國": "美國",
    "日本": "日本",
    "韓國": "韓國",
    "歐洲": "歐洲",
}


def parse_command_text(raw_text: str, user_id: str | None = None) -> CommandRequest:
    raw_text = raw_text.strip()
    if raw_text.startswith("/topic_import"):
        return _parse_topic_import(raw_text, user_id)

    tokens = shlex.split(raw_text)
    if not tokens:
        raise CommandParseError("參數格式錯誤，請使用 /help 查看指令格式。")

    command = tokens[0].lstrip("/").split("@", 1)[0]
    if command not in SUPPORTED_COMMANDS:
        raise CommandParseError("參數格式錯誤，請使用 /help 查看指令格式。")

    args = tokens[1:]
    positionals, flags = _split_args(args)
    request = _build_request(command, raw_text, positionals, flags, user_id)
    _validate_request(request, flags)
    return request


def _split_args(args: list[str]) -> tuple[list[str], dict[str, str | bool]]:
    positionals: list[str] = []
    flags: dict[str, str | bool] = {}
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("--"):
            positionals.append(token)
            index += 1
            continue

        name = token[2:].strip().replace("-", "_")
        if not name:
            raise CommandParseError("參數格式錯誤，請使用 /help 查看指令格式。")
        if name in {"date", "top", "model", "days", "source", "from_radar"}:
            if index + 1 >= len(args) or args[index + 1].startswith("--"):
                raise CommandParseError("參數格式錯誤，請使用 /help 查看指令格式。")
            flags[name] = args[index + 1]
            index += 2
        else:
            flags[name] = True
            index += 1
    return positionals, flags


def _build_request(command: str, raw_text: str, positionals: list[str], flags: dict[str, str | bool], user_id: str | None) -> CommandRequest:
    report_date = _parse_report_date(flags.get("date"))
    top = _parse_top(flags.get("top"))
    ai_model = _parse_ai_model(flags.get("model"))
    lookback_days = _parse_days(flags.get("days"))
    source = str(flags.get("source") or "").strip() or None
    source_only = bool(flags.get("source_only"))
    score = bool(flags.get("score"))
    brief = bool(flags.get("brief"))
    deep = bool(flags.get("deep"))
    mode = "source_only" if source_only else "score" if score else "brief" if brief else "deep" if deep else "normal"

    output_formats = _parse_output_formats(flags)

    common = {
        "command": command,
        "raw_text": raw_text,
        "mode": mode,
        "source_only": source_only,
        "score": score,
        "brief": brief,
        "top": top,
        "ai_model": ai_model,
        "source": source,
        "lookback_days": lookback_days,
        "report_date": report_date,
        "user_id": user_id,
        "created_at": datetime.now().astimezone(),
        "output_formats": output_formats,
    }

    if command == "research":
        target = " ".join(positionals).strip()
        if not target:
            raise CommandParseError("請輸入股票代號或名稱，例如 /research 2330")
        return CommandRequest(target=target, target_type="stock", **common)

    if command == "macro":
        market_scope = positionals[0] if positionals else "全球"
        theme_scope = positionals[1] if len(positionals) > 1 else None
        region_scope = REGION_MAP.get(market_scope, market_scope if market_scope != "global" else "global")
        return CommandRequest(market_scope=market_scope, theme_scope=theme_scope, region_scope=region_scope, **common)

    if command == "theme":
        theme = " ".join(positionals).strip()
        if not theme:
            raise CommandParseError("請輸入題材名稱，例如 /theme AI伺服器")
        return CommandRequest(target=theme, theme_scope=theme, target_type="theme", **common)

    if command == "theme_radar":
        target = " ".join(positionals).strip() or "market"
        return CommandRequest(target=target, target_type="theme_radar", **common)

    if command == "theme_flow":
        theme = " ".join(positionals).strip()
        if not theme:
            raise CommandParseError("請提供題材名稱，例如 /theme_flow AI伺服器")
        return CommandRequest(target=theme, theme_scope=theme, target_type="theme_flow", **common)

    if command == "sector_strength":
        target = " ".join(positionals).strip() or "market"
        return CommandRequest(target=target, target_type="sector_strength", **common)

    if command == "value_scan":
        first_arg = positionals[0] if positionals else None
        if first_arg and _looks_like_stock_code(first_arg):
            # 單檔模式：/value_scan 6217
            return CommandRequest(target=first_arg, candidate_pool=None, target_type="stock", **common)
        candidate_pool = " ".join(positionals).strip() or "精選選股"
        return CommandRequest(target=candidate_pool, candidate_pool=candidate_pool, target_type="candidate_pool", **common)

    if command == "report":
        target = _normalize_report_target(positionals)
        return CommandRequest(target=target, target_type="report", **common)

    # Topic commands
    if command == "topic_maintain":
        radar_id = str(flags.get("from_radar") or "").strip()
        theme = f"__from_radar__:{radar_id}" if radar_id else (" ".join(positionals).strip() if positionals else "")
        return CommandRequest(target=theme, theme_scope=theme, target_type="topic_maintain", **common)

    if command == "topic_review":
        change_id = positionals[0] if positionals else ""
        return CommandRequest(target=change_id, target_type="topic_review", **common)

    if command == "topic_confirm":
        change_id = positionals[0] if positionals else ""
        if not change_id:
            raise CommandParseError("/topic_confirm 需要 change_id，例如 /topic_confirm change_xxx")
        return CommandRequest(target=change_id, target_type="topic_confirm", **common)

    if command == "topic_reject":
        change_id = positionals[0] if positionals else ""
        if not change_id:
            raise CommandParseError("/topic_reject 需要 change_id，例如 /topic_reject change_xxx")
        return CommandRequest(target=change_id, target_type="topic_reject", **common)

    if command == "topic_profiles":
        return CommandRequest(target="", target_type="topic_profiles", **common)

    if command == "topic_reset":
        confirm = bool(flags.get("confirm"))
        return CommandRequest(target="__confirm__" if confirm else "", target_type="topic_reset", **common)

    if command == "topic_seed_prompt":
        theme = " ".join(positionals).strip() if positionals else ""
        return CommandRequest(target=theme, theme_scope=theme, target_type="topic_seed_prompt", **common)

    if command == "topic_import":
        payload = " ".join(positionals).strip()
        if not payload:
            raise CommandParseError("/topic_import 需要貼上外部 AI 產生的 JSON 內容")
        return CommandRequest(target=payload, target_type="topic_import", **common)

    if command == "topic_source_sync":
        only_tpex = bool(flags.get("tpex"))
        only_udn = bool(flags.get("udn"))
        target = "all"
        if only_tpex and not only_udn:
            target = "tpex"
        elif only_udn and not only_tpex:
            target = "udn"
        return CommandRequest(target=target, target_type="topic_source_sync", **common)

    if command == "news":
        target = positionals[0] if positionals else ""
        if target == "holdings":
            raise CommandParseError("/news holdings 已移除，請使用 /news latest 或 /news 7d 查看最後的庫存持股新聞區塊")
        return CommandRequest(target=target, target_type="news", **common)

    if command == "news_detail":
        news_id = positionals[0] if positionals else ""
        if not news_id:
            raise CommandParseError("/news_detail 需要 news_id，例如 /news_detail N123")
        return CommandRequest(target=news_id, target_type="news_detail", **common)

    if command in {"data_status", "news_status"}:
        target = " ".join(positionals).strip()
        return CommandRequest(target=target, target_type=command, **common)

    if command == "backfill_status":
        target = " ".join(positionals).strip()
        return CommandRequest(target=target, target_type="backfill_status", **common)

    raise CommandParseError("參數格式錯誤，請使用 /help 查看指令格式。")


def _parse_topic_import(raw_text: str, user_id: str | None) -> CommandRequest:
    """Parse /topic_import while preserving pasted JSON payload verbatim."""
    parts = raw_text.split(maxsplit=1)
    command = parts[0].lstrip("/").split("@", 1)[0]
    if command != "topic_import":
        raise CommandParseError("Unsupported topic import command")
    rest = parts[1].strip() if len(parts) > 1 else ""
    ai_model = "gemini"
    parsed_flags: dict[str, str | bool] = {}
    model_match = re.match(r"^--model\s+(\S+)\s*(.*)$", rest, flags=re.DOTALL)
    if model_match:
        ai_model = _parse_ai_model(model_match.group(1))
        parsed_flags["model"] = model_match.group(1)
        rest = model_match.group(2).strip()
    if not rest:
        raise CommandParseError("/topic_import 需要貼上外部 AI 產生的 JSON 內容")
    request = CommandRequest(
        command="topic_import",
        raw_text=raw_text,
        target=rest,
        target_type="topic_import",
        ai_model=ai_model,
        user_id=user_id,
        created_at=datetime.now().astimezone(),
        output_formats=("md", "html", "json"),
    )
    _validate_request(request, parsed_flags)
    return request


def _normalize_report_target(positionals: list[str]) -> str:
    if not positionals:
        return "__recent__"
    if len(positionals) == 1 and positionals[0] == "latest":
        return "latest"
    parts = [part for part in positionals if part != "latest"]
    if not parts:
        return "__recent__"
    return " ".join(parts).strip()


def _parse_output_formats(flags: dict[str, str | bool]) -> tuple[str, ...]:
    formats = ["md", "html", "json"]
    if flags.get("no_md"):
        formats.remove("md")
    if flags.get("no_html"):
        formats.remove("html")
    if flags.get("no_json"):
        formats.remove("json")
    if flags.get("html") and "html" not in formats:
        formats.append("html")
    if not formats:
        raise CommandParseError("??????????????????? --no-md?--no-html?--no-json?")
    return tuple(formats)


def _parse_report_date(value: str | bool | None) -> date | None:
    if value in (None, False):
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError as exc:
        raise CommandParseError("日期格式錯誤，請使用 YYYY-MM-DD，例如：--date 2026-01-07。") from exc


def _parse_top(value: str | bool | None) -> int | None:
    if value in (None, False):
        return None
    try:
        parsed = int(str(value))
    except ValueError as exc:
        raise CommandParseError("--top 參數需輸入正整數，例如：--top 30") from exc
    if parsed <= 0:
        raise CommandParseError("--top 參數需輸入正整數，例如：--top 30")
    return parsed


def _parse_days(value: str | bool | None) -> int | None:
    if value in (None, False):
        return None
    try:
        parsed = int(str(value))
    except ValueError as exc:
        raise CommandParseError("--days 必須是正整數，例如 --days 7") from exc
    if parsed <= 0:
        raise CommandParseError("--days 必須是正整數，例如 --days 7")
    return min(parsed, 60)


def _parse_ai_model(value: str | bool | None) -> str:
    if value in (None, False):
        return "gemini"
    normalized = str(value).strip().lower().replace("_", "-")
    aliases = {
        "gemini": "gemini",
        "google": "gemini",
        "deepseek": "deepseek",
        "opencode": "deepseek",
        "opencode-go": "deepseek",
        "deepseek-v4-pro": "deepseek",
        "minimax": "minimax",
        "minimax-m2.7": "minimax",
        "m2.7": "minimax",
    }
    if normalized not in aliases:
        raise CommandParseError("--model 僅支援 gemini、deepseek 或 minimax")
    return aliases[normalized]


def _validate_request(request: CommandRequest, flags: dict[str, str | bool]) -> None:
    if request.source_only and request.score:
        raise CommandParseError("--source-only 與 --score 不能同時使用。若要評分請移除 --source-only；若只要資料請移除 --score。")
    if request.source_only and request.brief:
        raise CommandParseError("--source-only 與 --brief 不能同時使用。若要資料請使用 --source-only；若要摘要請使用 --brief。")
    if request.command == "research" and request.top is not None:
        raise CommandParseError("/research 是單一個股研究，不支援 --top。若要排名請使用 /value_scan 或 /theme。")
    if request.brief and request.command != "macro":
        raise CommandParseError("--brief 目前僅支援 /macro 指令。")
    if request.score and request.command != "research":
        raise CommandParseError("--score 目前僅支援 /research 指令。")
    if request.top is not None and request.command not in {"theme", "value_scan", "theme_radar", "theme_flow", "sector_strength"}:
        raise CommandParseError("--top 目前僅支援 /theme 與 /value_scan 指令。")

    supported = {
        "research": {"source_only", "score", "deep", "date", "model", "html", "no_html", "no_md", "no_json"},
        "macro": {"source_only", "brief", "deep", "date", "model", "html", "no_html", "no_md", "no_json"},
        "theme": {"source_only", "deep", "date", "top", "model", "html", "no_html", "no_md", "no_json"},
        "theme_radar": {"source_only", "deep", "date", "top", "model", "days", "source", "html", "no_html", "no_md", "no_json"},
        "theme_flow": {"source_only", "deep", "date", "top", "model", "days", "source", "html", "no_html", "no_md", "no_json"},
        "sector_strength": {"source_only", "deep", "date", "top", "model", "days", "source", "html", "no_html", "no_md", "no_json"},
        "value_scan": {"source_only", "deep", "date", "top", "model", "html", "no_html", "no_md", "no_json"},
        "report": {"date", "html", "no_html", "no_md", "no_json"},
        "topic_maintain": {"deep", "bootstrap", "from_radar", "model", "html", "no_html", "no_md", "no_json"},
        "topic_review": set(),
        "topic_confirm": set(),
        "topic_reject": set(),
        "topic_profiles": set(),
        "topic_reset": {"confirm"},
        "topic_seed_prompt": {"model"},
        "topic_import": {"model"},
        "topic_source_sync": {"tpex", "udn"},
        "news": {"model"},
        "news_detail": set(),
        "data_status": {"date", "days"},
        "backfill_status": {"date"},
        "news_status": {"date", "days"},
    }
    unknown = set(flags) - supported.get(request.command, set())
    if unknown:
        raise CommandParseError("參數格式錯誤，請使用 /help 查看指令格式。")


def _looks_like_stock_code(text: str) -> bool:
    """判斷是否像股票代號（純數字 4-6 碼）。"""
    return bool(text) and text.isdigit() and 4 <= len(text) <= 6

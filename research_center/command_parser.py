from __future__ import annotations

import shlex
from datetime import date, datetime

from .models import CommandParseError, CommandRequest

SUPPORTED_COMMANDS = {"research", "macro", "theme", "value_scan", "report"}
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
    tokens = shlex.split(raw_text.strip())
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
        if name in {"date", "top"}:
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

    if command == "value_scan":
        candidate_pool = " ".join(positionals).strip() or "精選選股"
        return CommandRequest(target=candidate_pool, candidate_pool=candidate_pool, target_type="candidate_pool", **common)

    if command == "report":
        target = _normalize_report_target(positionals)
        return CommandRequest(target=target, target_type="report", **common)

    raise CommandParseError("參數格式錯誤，請使用 /help 查看指令格式。")


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
    if request.top is not None and request.command not in {"theme", "value_scan"}:
        raise CommandParseError("--top 目前僅支援 /theme 與 /value_scan 指令。")

    supported = {
        "research": {"source_only", "score", "deep", "date", "html", "no_html", "no_md", "no_json"},
        "macro": {"source_only", "brief", "deep", "date", "html", "no_html", "no_md", "no_json"},
        "theme": {"source_only", "deep", "date", "top", "html", "no_html", "no_md", "no_json"},
        "value_scan": {"source_only", "deep", "date", "top", "html", "no_html", "no_md", "no_json"},
        "report": {"date", "html", "no_html", "no_md", "no_json"},
    }
    unknown = set(flags) - supported.get(request.command, set())
    if unknown:
        raise CommandParseError("參數格式錯誤，請使用 /help 查看指令格式。")

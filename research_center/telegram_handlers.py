from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .models import CommandParseError
from .orchestrator import ResearchCenter
from .recent_scans import load_recent_scan_results

AI_CALLBACK_PREFIX = "ai_menu:"

RESEARCH_HELP_TEXT = """AI 投資研究中心指令：
/research 2330 [--source-only|--score|--deep] [--date YYYY-MM-DD]
/macro [台股] [AI] [--brief|--source-only|--deep] [--date YYYY-MM-DD]
/theme AI伺服器 [--top 20] [--source-only|--deep] [--date YYYY-MM-DD]
/value_scan [精選選股] [--top 30] [--source-only|--deep] [--date YYYY-MM-DD]
/report
/report latest

也可以只輸入 /research 2330 或 /value_scan，系統會顯示中文互動選單。"""


def build_research_handlers(safe_send_reply, safe_reply_document, run_stoppable_command, make_stoppable_handler):
    center = ResearchCenter()

    async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await safe_send_reply(update, RESEARCH_HELP_TEXT)

    async def run_ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        raw_text = _raw_command_text(update, context)
        if await _maybe_start_menu(update, context, raw_text, safe_send_reply):
            return
        await _execute_raw_command(update, context, center, raw_text, safe_send_reply, safe_reply_document)

    async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await run_ai_command(update, context)

    async def handle_ai_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query or not query.data or not query.data.startswith(AI_CALLBACK_PREFIX):
            return
        await query.answer()
        data = query.data[len(AI_CALLBACK_PREFIX):]
        state = context.user_data.setdefault("ai_menu", {})

        if data.startswith("research_mode:"):
            mode = data.split(":", 1)[1]
            state["mode"] = mode
            await query.edit_message_text("請選擇資料日期：", reply_markup=_date_keyboard("research"))
            return

        if data.startswith("macro_scope:"):
            scope = data.split(":", 1)[1]
            if scope == "custom":
                state["awaiting"] = "macro_scope"
                await query.edit_message_text("請輸入市場範圍，例如：台股電子股、美股科技股、台幣匯率與電子股")
                return
            state["market_scope"] = scope
            await query.edit_message_text("請選擇宏觀分析模式：", reply_markup=_macro_mode_keyboard())
            return

        if data.startswith("macro_mode:"):
            state["mode"] = data.split(":", 1)[1]
            await query.edit_message_text("請選擇資料日期：", reply_markup=_date_keyboard("macro"))
            return

        if data.startswith("theme_mode:"):
            state["mode"] = data.split(":", 1)[1]
            await query.edit_message_text("\u8acb\u9078\u64c7\u8cc7\u6599\u65e5\u671f\uff1a", reply_markup=_date_keyboard("theme"))
            return

        if data.startswith("value_source:"):
            source = data.split(":", 1)[1]
            if source == "recent":
                await query.edit_message_text("目前沒有已保存的最近掃描結果可選。請先執行 /scan，或改用精選選股名單。")
                return
            if source == "custom":
                state.clear()
                state.update({"command": "value_scan", "awaiting": "custom_codes"})
                await query.edit_message_text("請輸入自訂股票清單，例如：2330, 6217, 2308")
                return
            state["source"] = _value_source_label(source)
            if source == "portfolio":
                await query.edit_message_text("我的持股預設分析全部持股，請選擇分析模式：", reply_markup=_value_mode_keyboard())
                return
            if source == "curated":
                await query.edit_message_text("請選擇分析模式：", reply_markup=_value_mode_keyboard())
                return
            # 其他來源（all 等）直接進入模式選擇，不詢問幾檔
            await query.edit_message_text("請選擇分析模式：", reply_markup=_value_mode_keyboard())
            return

        if data.startswith("recent_scan:"):
            scan_id = data.split(":", 1)[1]
            state["source"] = "最近掃描:" + scan_id
            await query.edit_message_text("請選擇分析模式：", reply_markup=_value_mode_keyboard())
            return

        if data.startswith("value_mode:"):
            state["mode"] = data.split(":", 1)[1]
            await query.edit_message_text("請選擇資料日期：", reply_markup=_date_keyboard("value_scan"))
            return

        if data.startswith("date:"):
            _, command, choice = data.split(":", 2)
            if choice == "latest":
                state.pop("date", None)
                await query.edit_message_text("請選擇分析模型：", reply_markup=_analysis_model_keyboard())
                return
            state["awaiting"] = "date"
            await query.edit_message_text("請輸入日期，格式 YYYY-MM-DD，例如 2026-05-07")
            return

        if data.startswith("analysis_model:"):
            state["model"] = data.split(":", 1)[1]
            raw = _compose_menu_command(state)
            state.clear()
            await query.edit_message_text(f"已選擇分析模型，開始執行：\n{raw}")
            await _execute_raw_command(update, context, center, raw, safe_send_reply, safe_reply_document)
            return

    async def handle_ai_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        state = context.user_data.get("ai_menu") or {}
        awaiting = state.get("awaiting")
        if not awaiting:
            return
        text = (update.effective_message.text or "").strip() if update.effective_message else ""
        if awaiting == "custom_codes":
            state["source"] = "自訂:" + text
            state.pop("awaiting", None)
            await safe_send_reply(update, "自訂股票清單預設分析全部股票，請選擇分析模式：", reply_markup=_value_mode_keyboard())
            return
        if awaiting == "macro_scope":
            if not text:
                await safe_send_reply(update, "請輸入市場範圍，例如：台股電子股、美股科技股、台幣匯率與電子股")
                return
            state["market_scope"] = text
            state.pop("awaiting", None)
            await safe_send_reply(update, "已收到市場範圍，請選擇宏觀分析模式：", reply_markup=_macro_mode_keyboard())
            return
        if awaiting == "date":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                await safe_send_reply(update, "日期格式錯誤，請輸入 YYYY-MM-DD，例如 2026-05-07")
                return
            state["date"] = text
            state.pop("awaiting", None)
            await safe_send_reply(update, "請選擇分析模型：", reply_markup=_analysis_model_keyboard())

    return {
        "research": make_stoppable_handler("AI個股研究", run_ai_command),
        "macro": make_stoppable_handler("AI宏觀研究", run_ai_command),
        "theme": make_stoppable_handler("AI題材研究", run_ai_command),
        "value_scan": make_stoppable_handler("AI價值重估掃描", run_ai_command),
        "report": make_stoppable_handler("AI報告查詢", report_command),
        "ai_help": help_command,
        "ai_menu_callback": handle_ai_menu_callback,
        "ai_menu_text": handle_ai_menu_text,
    }


async def _maybe_start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str, safe_send_reply) -> bool:
    parts = raw_text.strip().split()
    if not parts:
        return False
    command = parts[0].lstrip("/").split("@", 1)[0]
    if command == "research" and len(parts) == 2:
        context.user_data["ai_menu"] = {"command": "research", "target": parts[1]}
        await safe_send_reply(update, "請選擇研究模式：", reply_markup=_research_mode_keyboard())
        return True
    if command == "macro" and len(parts) == 1:
        context.user_data["ai_menu"] = {"command": "macro"}
        await safe_send_reply(update, "請選擇市場範圍：", reply_markup=_macro_scope_keyboard())
        return True
    if command == "theme" and len(parts) >= 2 and not any(part.startswith("--") for part in parts[2:]):
        context.user_data["ai_menu"] = {"command": "theme", "theme": " ".join(parts[1:])}
        await safe_send_reply(update, "請選擇題材分析模式：", reply_markup=_theme_mode_keyboard())
        return True
    if command == "value_scan" and len(parts) == 1:
        context.user_data["ai_menu"] = {"command": "value_scan"}
        await safe_send_reply(update, "請選擇股票名單來源：", reply_markup=_value_source_keyboard())
        return True
    return False


async def _execute_raw_command(update: Update, context: ContextTypes.DEFAULT_TYPE, center: ResearchCenter, raw_text: str, safe_send_reply, safe_reply_document) -> None:
    user_id = str(update.effective_chat.id) if update.effective_chat else None
    _print_progress(raw_text, "收到 Telegram AI 投研指令")
    await safe_send_reply(update, "AI 投研任務已收到，正在整理資料與產生報告，請稍候...")

    def progress(message: str) -> None:
        _print_progress(raw_text, message)

    try:
        if center.should_use_parallel_model_reports(raw_text, user_id):
            result = await asyncio.to_thread(center.prepare_parallel_model_run, raw_text, user_id, progress)
        else:
            result = await asyncio.to_thread(center.run_text_command, raw_text, user_id, progress)
    except CommandParseError as exc:
        progress(f"指令解析失敗：{exc}")
        await safe_send_reply(update, f"❌ {exc}")
        return
    except Exception as exc:
        progress(f"AI 投研任務失敗：{exc}")
        await safe_send_reply(update, f"❌ AI 投研任務失敗：{exc}")
        return

    parallel_jobs = ((result.runtime_context or {}).get("parallel_model_jobs") or {}).get("model_jobs") or []
    if parallel_jobs:
        await safe_send_reply(update, f"{result.summary}\n\n模型狀態：" + "、".join(f"{job.get('model')}=pending" for job in parallel_jobs))
        for job in parallel_jobs:
            model_key = str(job.get("model_key") or "")
            if model_key:
                asyncio.create_task(_run_parallel_model_background(update, center, result, raw_text, model_key, safe_send_reply, safe_reply_document))
        return

    status_note = _telegram_status_note(result)
    await safe_send_reply(update, f"{result.summary}{status_note}")
    await _send_report_files(update, result, safe_send_reply, safe_reply_document)
    if (result.runtime_context or {}).get("minimax_comparison"):
        await safe_send_reply(update, "MiniMax-M2.7 比較報告已在背景開始產生；完成後會補傳，失敗也會另行通知。")
        asyncio.create_task(_run_minimax_comparison_background(update, center, result, raw_text, safe_send_reply, safe_reply_document))


def _research_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("一般研究", callback_data=f"{AI_CALLBACK_PREFIX}research_mode:normal"), InlineKeyboardButton("深度研究", callback_data=f"{AI_CALLBACK_PREFIX}research_mode:deep")],
        [InlineKeyboardButton("量化評分", callback_data=f"{AI_CALLBACK_PREFIX}research_mode:score"), InlineKeyboardButton("只看資料來源", callback_data=f"{AI_CALLBACK_PREFIX}research_mode:source_only")],
    ])


def _macro_scope_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("全球", callback_data=f"{AI_CALLBACK_PREFIX}macro_scope:全球"), InlineKeyboardButton("台股", callback_data=f"{AI_CALLBACK_PREFIX}macro_scope:台股")],
        [InlineKeyboardButton("美國", callback_data=f"{AI_CALLBACK_PREFIX}macro_scope:美國"), InlineKeyboardButton("中國", callback_data=f"{AI_CALLBACK_PREFIX}macro_scope:中國")],
        [InlineKeyboardButton("歐洲", callback_data=f"{AI_CALLBACK_PREFIX}macro_scope:歐洲"), InlineKeyboardButton("亞洲", callback_data=f"{AI_CALLBACK_PREFIX}macro_scope:亞洲")],
        [InlineKeyboardButton("手動輸入市場範圍", callback_data=f"{AI_CALLBACK_PREFIX}macro_scope:custom")],
    ])

def _macro_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("快速總覽", callback_data=f"{AI_CALLBACK_PREFIX}macro_mode:brief"), InlineKeyboardButton("一般宏觀", callback_data=f"{AI_CALLBACK_PREFIX}macro_mode:normal")],
        [InlineKeyboardButton("深度總經", callback_data=f"{AI_CALLBACK_PREFIX}macro_mode:deep"), InlineKeyboardButton("只看資料來源", callback_data=f"{AI_CALLBACK_PREFIX}macro_mode:source_only")],
    ])


def _theme_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("一般題材", callback_data=f"{AI_CALLBACK_PREFIX}theme_mode:normal"), InlineKeyboardButton("深度題材", callback_data=f"{AI_CALLBACK_PREFIX}theme_mode:deep")],
        [InlineKeyboardButton("只看資料來源", callback_data=f"{AI_CALLBACK_PREFIX}theme_mode:source_only")],
    ])



def _recent_scan_keyboard(records: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for item in records[:8]:
        label = f"{item.get('scan_type')} {item.get('report_date')}，{item.get('candidate_count')} 檔"
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"{AI_CALLBACK_PREFIX}recent_scan:{item.get('scan_id')}")])
    return InlineKeyboardMarkup(rows)

def _value_source_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("精選選股名單", callback_data=f"{AI_CALLBACK_PREFIX}value_source:curated"), InlineKeyboardButton("全市場初篩", callback_data=f"{AI_CALLBACK_PREFIX}value_source:all")],
        [InlineKeyboardButton("我的持股", callback_data=f"{AI_CALLBACK_PREFIX}value_source:portfolio"), InlineKeyboardButton("自訂股票清單", callback_data=f"{AI_CALLBACK_PREFIX}value_source:custom")],
        [InlineKeyboardButton("最近掃描結果", callback_data=f"{AI_CALLBACK_PREFIX}value_source:recent")],
    ])


def _value_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("一般重估", callback_data=f"{AI_CALLBACK_PREFIX}value_mode:normal"), InlineKeyboardButton("深度重估", callback_data=f"{AI_CALLBACK_PREFIX}value_mode:deep")],
        [InlineKeyboardButton("只看資料來源", callback_data=f"{AI_CALLBACK_PREFIX}value_mode:source_only")],
    ])


def _date_keyboard(command: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("最新日期", callback_data=f"{AI_CALLBACK_PREFIX}date:{command}:latest"), InlineKeyboardButton("指定日期", callback_data=f"{AI_CALLBACK_PREFIX}date:{command}:custom")],
    ])


def _analysis_model_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Gemini", callback_data=f"{AI_CALLBACK_PREFIX}analysis_model:gemini")],
        [InlineKeyboardButton("DeepSeek V4 Pro (OpenCode Go)", callback_data=f"{AI_CALLBACK_PREFIX}analysis_model:deepseek")],
    ])


def _compose_menu_command(state: dict) -> str:
    command = state.get("command")
    date_flag = f" --date {state['date']}" if state.get("date") else ""
    mode = state.get("mode") or "normal"
    mode_flag = " --deep" if mode == "deep" else " --score" if mode == "score" else " --source-only" if mode == "source_only" else ""
    model_flag = f" --model {state.get('model') or 'gemini'}"
    default_market = "\u5168\u7403"
    default_value_source = "\u7cbe\u9078\u9078\u80a1"
    if command == "research":
        return f"/research {state.get('target')}{mode_flag}{date_flag}{model_flag}"
    if command == "macro":
        return f"/macro {state.get('market_scope') or default_market}{mode_flag}{date_flag}{model_flag}"
    if command == "theme":
        return f"/theme {state.get('theme')}{mode_flag}{date_flag}{model_flag}"
    if command == "value_scan":
        # 不再附加 --top，回歸資料服務層的內建限制（一般 10、deep 30）
        return f"/value_scan {state.get('source') or default_value_source}{mode_flag}{date_flag}{model_flag}"
    return "/report"


def _value_source_label(source: str) -> str:
    return {
        "curated": "精選選股",
        "all": "全市場初篩",
        "portfolio": "我的持股",
    }.get(source, source)


def _raw_command_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    if update.effective_message and update.effective_message.text:
        return update.effective_message.text
    command = context.match.string if getattr(context, "match", None) else ""
    return command or ""


async def _send_report_files(update: Update, result, safe_send_reply, safe_reply_document) -> None:
    for path, caption in ((result.artifacts.markdown_path, "Markdown 完整報告"), (result.artifacts.html_path, "HTML 完整報告")):
        await _send_existing_report_file(update, path, caption, safe_send_reply, safe_reply_document)

    comparison_reports = ((result.report_json or {}).get("metadata") or {}).get("comparison_reports") or []
    for report in comparison_reports:
        await _send_comparison_report_entry(update, report, safe_send_reply, safe_reply_document)


async def _run_parallel_model_background(update: Update, center: ResearchCenter, result, raw_text: str, model_key: str, safe_send_reply, safe_reply_document) -> None:
    def progress(message: str) -> None:
        _print_progress(raw_text, message)

    try:
        entry = await asyncio.to_thread(center.run_parallel_model_job, result, model_key, progress)
    except Exception as exc:
        progress(f"Parallel model task crashed: {model_key}: {exc}")
        await safe_send_reply(update, f"⚠️ {model_key} 報告產出失敗。\n原因：{exc}")
        return
    model = entry.get("model") or model_key
    status = entry.get("status")
    if status == "success":
        await safe_send_reply(update, f"✅ {model} 報告已產生，正在傳送附件。")
        await _send_model_report_entry(update, entry, safe_send_reply, safe_reply_document)
        return
    reason = entry.get("error") or entry.get("reason") or "未知原因"
    prompt_path = entry.get("prompt_path")
    path_note = f"\nPrompt：{prompt_path}" if prompt_path else ""
    await safe_send_reply(update, f"⚠️ {model} 報告產出失敗。\n原因：{reason}{path_note}")


async def _send_model_report_entry(update: Update, report: dict, safe_send_reply, safe_reply_document) -> None:
    for key, caption in (("markdown_path", f"{report.get('model') or 'AI'} 報告 Markdown"), ("html_path", f"{report.get('model') or 'AI'} 報告 HTML")):
        raw_path = str(report.get(key) or "").strip()
        if not raw_path:
            continue
        await _send_existing_report_file(update, Path(raw_path), caption, safe_send_reply, safe_reply_document)

async def _run_minimax_comparison_background(update: Update, center: ResearchCenter, result, raw_text: str, safe_send_reply, safe_reply_document) -> None:
    def progress(message: str) -> None:
        _print_progress(raw_text, message)

    try:
        entry = await asyncio.to_thread(center.run_minimax_comparison_for_result, result, progress)
    except Exception as exc:
        progress(f"MiniMax comparison background task crashed: {exc}")
        await safe_send_reply(update, f"⚠️ MiniMax-M2.7 比較報告產出失敗。\n原因：{exc}")
        return
    model = entry.get("model") or "MiniMax-M2.7"
    status = entry.get("status")
    if status == "success":
        await safe_send_reply(update, f"✅ {model} 比較報告已產生，正在傳送附件。")
        await _send_comparison_report_entry(update, entry, safe_send_reply, safe_reply_document)
        return
    reason = entry.get("error") or entry.get("reason") or "未知原因"
    prompt_path = entry.get("prompt_path")
    path_note = f"\nPrompt：{prompt_path}" if prompt_path else ""
    await safe_send_reply(update, f"⚠️ {model} 比較報告產出失敗。\n原因：{reason}{path_note}")


async def _send_comparison_report_entry(update: Update, report: dict, safe_send_reply, safe_reply_document) -> None:
    status = report.get("status")
    if status in {"failed", "pending", "skipped"} or report.get("error"):
        return
    model = report.get("model") or "MiniMax"
    for key, caption in (("markdown_path", f"{model} 比較報告 Markdown"), ("html_path", f"{model} 比較報告 HTML")):
        raw_path = str(report.get(key) or "").strip()
        if not raw_path:
            continue
        await _send_existing_report_file(update, Path(raw_path), caption, safe_send_reply, safe_reply_document)


async def _send_existing_report_file(update: Update, path: Path, caption: str, safe_send_reply, safe_reply_document) -> None:
    if not path.exists() or not path.is_file():
        return
    try:
        with path.open("rb") as handle:
            await safe_reply_document(update, handle, path.name, caption)
    except Exception as exc:
        await safe_send_reply(update, f"⚠️ {caption} 已產生但傳送失敗：{path}\n原因：{exc}")
def _telegram_status_note(result) -> str:
    notes: list[str] = []
    if result.ai_used and result.ai_model:
        notes.append(f"AI 模型：{result.ai_model}")
    elif result.request.source_only:
        notes.append("AI 模型：未調用（source-only 模式）")
    elif result.fallback_reason:
        notes.append("AI 模型：調用失敗，已改用本地 fallback")
    if result.fallback_reason:
        notes.append(f"⚠️ Gemini / 公開網路搜尋未完整成功，本報告已使用本地資料 fallback。原因：{result.fallback_reason}")
    return "\n\n" + "\n".join(notes) if notes else ""


def _print_progress(raw_text: str, message: str) -> None:
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    command = raw_text.splitlines()[0][:120] if raw_text else "AI投研"
    print(f"[{timestamp}] [AI投研] {command} | {message}", flush=True)









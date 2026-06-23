from __future__ import annotations

import asyncio
import io
import re
from datetime import datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .models import CommandParseError
from .orchestrator import ResearchCenter
from .recent_scans import load_recent_scan_results

RESEARCH_HELP_TEXT = """台股 AI 助理完整指令

模型參數: --model gemini|deepseek|minimax；MiniMax M3 適合長文搜尋整理，DeepSeek V4 Pro 適合推理與摘要。

選股與雷達
/scan - 開啟選股掃描選單
/scan 2026-05-22 - 指定日期掃描
/radar - 今日選股雷達
/radar --model deepseek - 指定模型產生雷達短評
/radar --no-ai-comment - 不產生 AI 短評
/radar_more - 查看最近一次 Radar 完整名單
/radar_more 2026-05-22 - 查看指定日期 Radar

個股與價值分析
/research - 互動式個股研究
/research 2330 - 直接研究個股
/research 2330 --score - 個股評分研究
/research 2330 --deep - 深度個股研究
/research 2330 --date 2026-05-22 - 指定日期研究
/research 2330 --model minimax - 指定模型
/value_scan - 開啟價值重估掃描選單
/value_scan 精選選股 - 掃描精選選股
/value_scan 我的持股 - 掃描持股
/value_scan 2330 - 單股價值重估
/value_scan 精選選股 --top 30 --model deepseek - 指定數量與模型

市場與新聞
/news - 新聞選單
/news latest - 最新新聞
/news 7d - 近 7 天新聞
/news refresh --model deepseek - 更新新聞庫
/macro - 宏觀研究選單
/macro 台股 - 台股宏觀研究
/macro 全球 AI --deep --model minimax - 指定主題、模式與模型

題材與族群
/theme - 題材研究選單
/theme AI伺服器 - 題材研究
/theme AI伺服器 --deep --top 20 - 深度題材研究
/theme_radar - 市場題材雷達，互動選日期與模型
/theme_radar --days 7 - 近 7 天題材統計
/theme_radar --date 2026-05-22 --model minimax - 指定日期與模型
/theme_flow AI伺服器 [--date 2026-05-22] [--model deepseek] - 題材擴散路徑
/sector_strength [--date 2026-05-22] [--model deepseek] - 族群強弱排行

題材庫維護
/topic_maintain - 大範圍完整維護題材庫，互動選模型
/topic_seed_prompt - 產生外部高階 AI 題材庫提示詞
/topic_import - 匯入外部 AI JSON（本地轉成變更包，不呼叫 AI）
/topic_source_sync - 同步 TPEx/UDN 外部來源並套用正式題材庫
/topic_source_sync --tpex - 只同步 TPEx 產業鏈
/topic_source_sync --udn - 只同步 UDN 產業資料庫
/topic_review - 查看所有變更包
/topic_review change_xxx - 查看指定變更包
/topic_confirm change_xxx - 套用變更包
/topic_reject change_xxx - 拒絕變更包
/topic_profiles - 查看正式題材庫
/topic_reset --confirm - 備份後清空題材庫

持股與監控
/my - 查看我的持股
/in 2330 - 加入持股
/out 2330 - 移除持股
/list_m - 查看監控清單
/add_m 2330 - 加入監控
/del_m 2330 - 移除監控
/check - 執行監控掃描

資料回補與匯出
/backfill - 回補本地資料
/backfill 2026-05-22 - 指定日期回補
/backfill 2026-05-22 force - 強制回補
/data_status 2330 - 查詢個股 Feature Pack / 資料覆蓋狀態
/backfill_status - 查詢最近回補 marker 與快取健康度
/news_status 2330 - 查詢新聞庫保存狀態
/export 2330 - 匯出股票資料
/stock_chart 2330 2026-01-01 2026-05-01 1d - 匯出個股圖表
/tmf_chart 2026-05-01 2026-05-05 1m - 匯出 TMF 圖表

報告與系統
/report - 查看最近報告清單
/report latest - 查看最近一份報告
/report 2330 latest - 查看個股最近報告
/report theme AI伺服器 latest - 查看題材最近報告
/morning - 晨報
/noon - 午報
/tw_market - 台股午報
/help - 完整指令說明
/stop - 停止目前任務
"""

AI_CALLBACK_PREFIX = "ai_menu:"
TOPIC_IMPORT_MAX_FILE_SIZE_BYTES = 10_000_000
TOPIC_IMPORT_MAX_FILE_SIZE_MB = TOPIC_IMPORT_MAX_FILE_SIZE_BYTES // 1_000_000
LONG_RUNNING_COMMANDS = {
    "research",
    "macro",
    "theme",
    "theme_radar",
    "theme_flow",
    "sector_strength",
    "value_scan",
    "topic_maintain",
}


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

    async def news_save_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        raw_text = _raw_command_text(update, context)
        await _save_news_url_from_text(update, raw_text)

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
            if state.get("date_first") and state.get("date_selected"):
                await query.edit_message_text("請選擇分析模型：", reply_markup=_analysis_model_keyboard())
                return
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
            await query.edit_message_text("請選擇資料日期：", reply_markup=_date_keyboard("theme"))
            return

        if data.startswith("value_source:"):
            source = data.split(":", 1)[1]
            if source == "recent":
                records = load_recent_scan_results(limit=8)
                if not records:
                    await query.edit_message_text("目前沒有已保存的最近掃描結果可選。請先執行 /scan，或改用精選選股名單。")
                    return
                await query.edit_message_text("請選擇最近掃描結果：", reply_markup=_recent_scan_keyboard(records))
                return
            if source == "custom":
                state.clear()
                state.update({"command": "value_scan", "awaiting": "custom_codes"})
                await query.edit_message_text("請輸入自訂股票清單，例如：2330, 6217, 2308")
                return
            if source == "single":
                state.clear()
                state.update({"command": "value_scan", "awaiting": "single_stock"})
                await query.edit_message_text("請輸入單一股票代號或名稱，例如：6282、康舒")
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
                state["date_selected"] = True
                if command == "research" and state.get("date_first") and not state.get("mode"):
                    await query.edit_message_text("請選擇研究模式：", reply_markup=_research_mode_keyboard())
                    return
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

        # ── /news model selection (layer 1) ────────────────────────────────
        if data.startswith("news_model:"):
            model = data.split(":", 1)[1]  # "gemini", "deepseek", "minimax"
            raw = _compose_news_command(model, "refresh")
            state.clear()
            await query.edit_message_text(f"📰 執行新聞更新：{raw}")
            await _execute_raw_command(update, context, center, raw, safe_send_reply, safe_reply_document)
            return

        # ── /news action selection (layer 2) ───────────────────────────────
        if data.startswith("news_action:"):
            action = data.split(":", 1)[1]  # "latest", "7d", "refresh"
            if action == "refresh":
                state["awaiting"] = "news_model"
                await query.edit_message_text("📰 搜尋並更新新聞\n\n請選擇 AI 分類模型：", reply_markup=_news_model_keyboard())
                return
            raw = _compose_news_command("gemini", action)
            state.clear()
            await query.edit_message_text(f"📰 執行新聞動作：{raw}")
            await _execute_raw_command(update, context, center, raw, safe_send_reply, safe_reply_document)
            return

        # ── /topic_maintain model selection ───────────────────────────────
        if data.startswith("topic_maintain:model:"):
            # data: topic_maintain:model:gemini
            model = data.split(":", 2)[2]
            state["mode"] = "deep"
            state["model"] = model
            raw = _compose_topic_maintain_command(state)
            state.clear()
            await query.edit_message_text(f"已選擇，開始執行：\n{raw}")
            await _execute_raw_command(update, context, center, raw, safe_send_reply, safe_reply_document)
            return

        if data == "topic_import:confirm":
            payload = str(state.get("payload") or "").strip()
            state.clear()
            if not payload:
                await query.edit_message_text("匯入內容是空的，請重新輸入 /topic_import 後貼上外部 AI JSON。")
                return
            raw = f"/topic_import {payload}"
            await query.edit_message_text("已確認匯入，開始建立題材變更包：\n/topic_import")
            await _execute_raw_command(update, context, center, raw, safe_send_reply, safe_reply_document)
            return

        if data == "topic_import:cancel":
            state.clear()
            await query.edit_message_text("已取消匯入。")
            return

        if data.startswith("topic_action:"):
            parts = data.split(":", 2)
            if len(parts) != 3:
                await query.edit_message_text("題材操作格式錯誤，請重新使用 /topic_review 查看。")
                return
            action, change_id = parts[1], parts[2].strip()
            command_map = {
                "confirm": "/topic_confirm",
                "reject": "/topic_reject",
                "review": "/topic_review",
            }
            command = command_map.get(action)
            if not command or not change_id:
                await query.edit_message_text("題材操作格式錯誤，請重新使用 /topic_review 查看。")
                return
            raw = f"{command} {change_id}"
            await query.edit_message_text(f"已選擇操作，開始執行：\n{raw}")
            await _execute_raw_command(update, context, center, raw, safe_send_reply, safe_reply_document)
            return

    async def _save_news_url_from_text(update: Update, text: str):
        url = _extract_first_url(text)
        if not url:
            return
        await safe_send_reply(update, "已收到新聞連結，正在抓取與保存...")
        from .news_formatters import format_news_detail
        from .news_repository import NewsRepository
        from .news_service import save_user_submitted_news_url

        repository = NewsRepository(center.config.database_path)
        item, status = await asyncio.to_thread(
            save_user_submitted_news_url,
            url,
            center,
            repository,
            None,
            "gemini",
        )
        prefix = {
            "saved": "已保存到新聞庫：",
            "duplicate": "這則新聞已經保存過：",
            "invalid_url": "這個網址格式不正確，未保存。",
            "non_article_page": "這個網址不像單篇新聞文章，未保存。",
            "fetch_failed": "無法抓取這個網址內容，未保存。",
            "not_taiwan_finance_news": "這則內容不像台股、台灣財經或產業新聞，未保存。",
        }.get(status, f"未保存：{status}")
        if item is not None and status in {"saved", "duplicate"}:
            await safe_send_reply(update, f"{prefix}\n\n{format_news_detail(item)}")
        else:
            await safe_send_reply(update, prefix)

    async def handle_news_url_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.effective_message.text or "").strip() if update.effective_message else ""
        await _save_news_url_from_text(update, text)

    async def handle_ai_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        state = context.user_data.get("ai_menu") or {}
        awaiting = state.get("awaiting")
        text = (update.effective_message.text or "").strip() if update.effective_message else ""
        if not awaiting:
            url = _extract_first_url(text)
            if url:
                await safe_send_reply(update, "已收到新聞連結，正在抓取與保存...")
                from .news_formatters import format_news_detail
                from .news_repository import NewsRepository
                from .news_service import save_user_submitted_news_url

                repository = NewsRepository(center.config.database_path)
                item, status = await asyncio.to_thread(
                    save_user_submitted_news_url,
                    url,
                    center,
                    repository,
                    None,
                    "gemini",
                )
                prefix = {
                    "saved": "已保存到新聞庫：",
                    "duplicate": "新聞庫已有這則新聞：",
                    "invalid_url": "這看起來不是有效網址。",
                    "non_article_page": "這個網址不像單篇新聞文章，未保存。",
                    "fetch_failed": "無法抓取這個網址內容，未保存。",
                    "not_taiwan_finance_news": "這則內容不像台股、台灣財經或產業新聞，未保存。",
                }.get(status, f"未保存：{status}")
                if item is not None and status in {"saved", "duplicate"}:
                    await safe_send_reply(update, f"{prefix}\n\n{format_news_detail(item)}")
                else:
                    await safe_send_reply(update, prefix)
                return
            return
        if awaiting == "custom_codes":
            state["source"] = "自訂:" + text
            state.pop("awaiting", None)
            await safe_send_reply(update, "自訂股票清單預設分析全部股票，請選擇分析模式：", reply_markup=_value_mode_keyboard())
            return
        if awaiting == "single_stock":
            if not text:
                await safe_send_reply(update, "請輸入單一股票代號或名稱，例如：6282、康舒")
                return
            state["source"] = text
            state.pop("awaiting", None)
            await safe_send_reply(update, "單一股票重估已收到，請選擇分析模式：", reply_markup=_value_mode_keyboard())
            return
        if awaiting == "research_target":
            if not text:
                await safe_send_reply(update, "請輸入股票代號或名稱，例如：2330、台積電")
                return
            state["target"] = text
            state["date_first"] = True
            state.pop("awaiting", None)
            await safe_send_reply(update, "請選擇資料日期：", reply_markup=_date_keyboard("research"))
            return
        if awaiting == "theme_query":
            if not text:
                await safe_send_reply(update, "請輸入題材或產業，例如：AI電源、重電、機器人")
                return
            state["theme"] = " ".join(text.split())
            state.pop("awaiting", None)
            await safe_send_reply(update, "已收到題材或產業，請選擇題材分析模式：", reply_markup=_theme_mode_keyboard())
            return
        if awaiting == "theme_flow_query":
            if not text:
                await safe_send_reply(update, "請輸入題材或產業，例如：AI電源、記憶體、BBU")
                return
            state["theme"] = " ".join(text.split())
            state.pop("awaiting", None)
            await safe_send_reply(update, "已收到題材或產業，請選擇資料日期：", reply_markup=_date_keyboard("theme_flow"))
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
            state["date_selected"] = True
            state.pop("awaiting", None)
            if state.get("command") == "research" and state.get("date_first") and not state.get("mode"):
                await safe_send_reply(update, "請選擇研究模式：", reply_markup=_research_mode_keyboard())
                return
            await safe_send_reply(update, "請選擇分析模型：", reply_markup=_analysis_model_keyboard())
            return
        if awaiting == "topic_import_payload":
            if not text:
                await safe_send_reply(update, "請貼上外部 AI 產生的 JSON 內容，或上傳 .json/.txt 檔。")
                return
            state["payload"] = text
            state.pop("awaiting", None)
            await safe_send_reply(update, _topic_import_confirm_text(text), reply_markup=_topic_import_confirm_keyboard())
            return

    async def handle_ai_menu_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
        state = context.user_data.get("ai_menu") or {}
        if state.get("awaiting") != "topic_import_payload":
            return
        message = update.effective_message
        document = message.document if message else None
        if not document:
            return
        if document.file_size and document.file_size > TOPIC_IMPORT_MAX_FILE_SIZE_BYTES:
            await safe_send_reply(update, f"檔案太大，請上傳 {TOPIC_IMPORT_MAX_FILE_SIZE_MB}MB 以下的 JSON 或文字檔。")
            return
        file_obj = await document.get_file()
        data = bytes(await file_obj.download_as_bytearray())
        try:
            payload = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            payload = data.decode("utf-8", errors="replace")
        state["payload"] = payload.strip()
        state.pop("awaiting", None)
        await safe_send_reply(update, _topic_import_confirm_text(state["payload"], from_file=True), reply_markup=_topic_import_confirm_keyboard())
        return

    return {
        "theme_radar": make_stoppable_handler("市場題材雷達", run_ai_command),
        "theme_flow": make_stoppable_handler("題材擴散路徑", run_ai_command),
        "sector_strength": make_stoppable_handler("傳統類股強弱", run_ai_command),
        "research": make_stoppable_handler("AI個股研究", run_ai_command),
        "macro": make_stoppable_handler("AI宏觀研究", run_ai_command),
        "theme": make_stoppable_handler("AI題材研究", run_ai_command),
        "value_scan": make_stoppable_handler("AI價值重估掃描", run_ai_command),
        "news": make_stoppable_handler("新聞查詢", run_ai_command),
        "news_detail": make_stoppable_handler("新聞詳情", run_ai_command),
        "news_save": news_save_command,
        "data_status": make_stoppable_handler("AI資料狀態", run_ai_command),
        "backfill_status": make_stoppable_handler("AI回補狀態", run_ai_command),
        "news_status": make_stoppable_handler("新聞庫狀態", run_ai_command),
        "report": make_stoppable_handler("AI報告查詢", report_command),
        "topic_maintain": make_stoppable_handler("AI題材庫維護", run_ai_command),
        "topic_review": make_stoppable_handler("AI題材庫檢視", run_ai_command),
        "topic_confirm": make_stoppable_handler("AI題材庫確認", run_ai_command),
        "topic_reject": make_stoppable_handler("AI題材庫拒絕", run_ai_command),
        "topic_profiles": make_stoppable_handler("AI題材庫查詢", run_ai_command),
        "topic_reset": make_stoppable_handler("AI題材庫重置", run_ai_command),
        "topic_seed_prompt": make_stoppable_handler("AI題材庫外部提示詞", run_ai_command),
        "topic_import": make_stoppable_handler("AI題材庫外部匯入", run_ai_command),
        "topic_source_sync": make_stoppable_handler("AI題材庫外部來源同步", run_ai_command),
        "help": help_command,
        "ai_help": help_command,
        "ai_menu_callback": handle_ai_menu_callback,
        "ai_menu_text": handle_ai_menu_text,
        "news_url_message": handle_news_url_message,
        "ai_menu_document": handle_ai_menu_document,
    }


async def _maybe_start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str, safe_send_reply) -> bool:
    parts = raw_text.strip().split()
    if not parts:
        return False
    command = parts[0].lstrip("/").split("@", 1)[0]
    if command == "research" and len(parts) == 1:
        context.user_data["ai_menu"] = {"command": "research", "awaiting": "research_target"}
        await safe_send_reply(update, "請輸入股票代號或名稱，例如：2330、台積電")
        return True
    if command == "research" and len(parts) == 2:
        return False
    if command == "macro" and len(parts) == 1:
        context.user_data["ai_menu"] = {"command": "macro"}
        await safe_send_reply(update, "請選擇市場範圍：", reply_markup=_macro_scope_keyboard())
        return True
    if command == "theme" and len(parts) == 1:
        context.user_data["ai_menu"] = {"command": "theme", "awaiting": "theme_query"}
        await safe_send_reply(update, "請輸入題材或產業，例如：AI電源、重電、機器人")
        return True
    if command == "theme" and len(parts) >= 2 and not any(part.startswith("--") for part in parts[2:]):
        context.user_data["ai_menu"] = {"command": "theme", "theme": " ".join(parts[1:])}
        await safe_send_reply(update, "請選擇題材分析模式：", reply_markup=_theme_mode_keyboard())
        return True
    if command == "value_scan" and len(parts) == 1:
        context.user_data["ai_menu"] = {"command": "value_scan"}
        await safe_send_reply(update, "請選擇股票名單來源：", reply_markup=_value_source_keyboard())
        return True
    if command == "theme_radar" and len(parts) == 1:
        context.user_data["ai_menu"] = {"command": "theme_radar"}
        await safe_send_reply(update, "請選擇題材雷達資料日期：", reply_markup=_date_keyboard("theme_radar"))
        return True
    if command == "theme_flow" and len(parts) == 1:
        context.user_data["ai_menu"] = {"command": "theme_flow", "awaiting": "theme_flow_query"}
        await safe_send_reply(update, "請輸入題材或產業，例如：AI電源、記憶體、BBU")
        return True
    if command == "theme_flow" and len(parts) >= 2 and not any(part.startswith("--") for part in parts[2:]):
        context.user_data["ai_menu"] = {"command": "theme_flow", "theme": " ".join(parts[1:])}
        await safe_send_reply(update, "請選擇資料日期：", reply_markup=_date_keyboard("theme_flow"))
        return True
    if command == "sector_strength" and len(parts) == 1:
        context.user_data["ai_menu"] = {"command": "sector_strength"}
        await safe_send_reply(update, "請選擇族群強弱資料日期：", reply_markup=_date_keyboard("sector_strength"))
        return True
    # /topic_maintain with no args -> show model selection menu directly (default full maintenance)
    if command == "topic_maintain" and (
        len(parts) == 1
    ):
        context.user_data["ai_menu"] = {"command": "topic_maintain"}
        await safe_send_reply(update, "請選擇 AI 模型：", reply_markup=_topic_model_keyboard())
        return True
    if command == "topic_import" and len(parts) == 1:
        context.user_data["ai_menu"] = {"command": "topic_import", "awaiting": "topic_import_payload"}
        await safe_send_reply(update, "請貼上外部 AI 產生的 JSON，或上傳 .json/.txt 檔。")
        return True
    # /news with no args -> show action selection first. Only refresh asks for model.
    if command == "news" and len(parts) == 1:
        context.user_data["ai_menu"] = {"command": "news"}
        await safe_send_reply(update, "📰 新聞中心\n\n請選擇新聞動作：", reply_markup=_news_action_keyboard())
        return True
    return False


async def _execute_raw_command(update: Update, context: ContextTypes.DEFAULT_TYPE, center: ResearchCenter, raw_text: str, safe_send_reply, safe_reply_document) -> None:
    user_id = str(update.effective_chat.id) if update.effective_chat else None
    _print_progress(raw_text, "收到 Telegram AI 投研指令")
    await safe_send_reply(update, "AI 投研任務已收到，正在整理資料與產生報告，請稍候...")

    heartbeat = None
    if _should_use_progress_heartbeat(raw_text):
        from progress_logger import ProgressHeartbeat

        heartbeat = ProgressHeartbeat(
            _heartbeat_label(raw_text),
            sink=lambda message: _print_progress(raw_text, message),
        ).start()
        try:
            from backfill_service import is_backfill_running

            if is_backfill_running():
                notice = "偵測到完整資料回補正在執行，本次任務會優先使用既有快取與逾時降級，避免長時間等待資料源。"
                heartbeat.update(notice, stage="回補狀態檢查")
                _print_progress(raw_text, notice)
        except Exception:
            pass

    def progress(message: str) -> None:
        if heartbeat:
            heartbeat.update(message)
        _print_progress(raw_text, message)

    try:
        if center.should_use_parallel_model_reports(raw_text, user_id):
            result = await asyncio.to_thread(center.prepare_parallel_model_run, raw_text, user_id, progress)
        else:
            result = await asyncio.to_thread(center.run_text_command, raw_text, user_id, progress)
    except asyncio.CancelledError:
        if heartbeat:
            heartbeat.stop()
        raise
    except CommandParseError as exc:
        progress(f"指令解析失敗：{exc}")
        if heartbeat:
            heartbeat.stop()
        await safe_send_reply(update, f"❌ {exc}")
        return
    except Exception as exc:
        progress(f"AI 投研任務失敗：{exc}")
        if heartbeat:
            heartbeat.stop()
        await safe_send_reply(update, f"❌ AI 投研任務失敗：{exc}")
        return

    if heartbeat:
        heartbeat.stop()

    parallel_jobs = ((result.runtime_context or {}).get("parallel_model_jobs") or {}).get("model_jobs") or []
    if parallel_jobs:
        await safe_send_reply(update, f"{result.summary}\n\n模型狀態：" + "、".join(f"{job.get('model')}=pending" for job in parallel_jobs))
        for job in parallel_jobs:
            model_key = str(job.get("model_key") or "")
            if model_key:
                asyncio.create_task(_run_parallel_model_background(update, center, result, raw_text, model_key, safe_send_reply, safe_reply_document))
        return

    if _is_topic_result(result):
        await send_topic_result_reply(update, result, safe_send_reply)
    else:
        status_note = _telegram_status_note(result)
        await safe_send_reply(update, f"{result.summary}{status_note}")
    await _send_runtime_document(update, result, safe_reply_document)
    await _send_report_files(update, result, safe_send_reply, safe_reply_document)
    if (result.runtime_context or {}).get("minimax_comparison"):
        await safe_send_reply(update, "MiniMax-M3 比較報告已在背景開始產生；完成後會補傳，失敗也會另行通知。")
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
        [InlineKeyboardButton("精選選股", callback_data=f"{AI_CALLBACK_PREFIX}value_source:curated"), InlineKeyboardButton("選股雷達", callback_data=f"{AI_CALLBACK_PREFIX}value_source:radar")],
        [InlineKeyboardButton("我的持股", callback_data=f"{AI_CALLBACK_PREFIX}value_source:portfolio"), InlineKeyboardButton("監控清單", callback_data=f"{AI_CALLBACK_PREFIX}value_source:monitor")],
        [InlineKeyboardButton("最近掃描結果", callback_data=f"{AI_CALLBACK_PREFIX}value_source:recent"), InlineKeyboardButton("自訂股票清單", callback_data=f"{AI_CALLBACK_PREFIX}value_source:custom")],
        [InlineKeyboardButton("單一股票", callback_data=f"{AI_CALLBACK_PREFIX}value_source:single"), InlineKeyboardButton("全市場初篩", callback_data=f"{AI_CALLBACK_PREFIX}value_source:all")],
    ])


def _value_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("一般重估", callback_data=f"{AI_CALLBACK_PREFIX}value_mode:normal"), InlineKeyboardButton("深度重估", callback_data=f"{AI_CALLBACK_PREFIX}value_mode:deep")],
        [InlineKeyboardButton("只看資料來源", callback_data=f"{AI_CALLBACK_PREFIX}value_mode:source_only")],
    ])


def _date_keyboard(command: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("最新日期", callback_data=f"{AI_CALLBACK_PREFIX}date:{command}:latest"),
            InlineKeyboardButton("指定日期", callback_data=f"{AI_CALLBACK_PREFIX}date:{command}:custom"),
        ],
    ])


def _analysis_model_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Gemini", callback_data=f"{AI_CALLBACK_PREFIX}analysis_model:gemini")],
        [InlineKeyboardButton("DeepSeek V4 Pro (OpenCode Go)", callback_data=f"{AI_CALLBACK_PREFIX}analysis_model:deepseek")],
        [InlineKeyboardButton("MiniMax M3", callback_data=f"{AI_CALLBACK_PREFIX}analysis_model:minimax")],
    ])


def _news_menu_keyboard() -> InlineKeyboardMarkup:
    """Entry point for /news: choose action first."""
    return _news_action_keyboard()


def _news_model_keyboard() -> InlineKeyboardMarkup:
    """Layer 1: model selection for /news."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Gemini", callback_data=f"{AI_CALLBACK_PREFIX}news_model:gemini")],
        [InlineKeyboardButton("DeepSeek V4 Pro (OpenCode Go)", callback_data=f"{AI_CALLBACK_PREFIX}news_model:deepseek")],
        [InlineKeyboardButton("MiniMax M3", callback_data=f"{AI_CALLBACK_PREFIX}news_model:minimax")],
    ])


def _news_action_keyboard() -> InlineKeyboardMarkup:
    """Action selection for /news. Refresh will ask for model next."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📰 最新新聞", callback_data=f"{AI_CALLBACK_PREFIX}news_action:latest")],
        [InlineKeyboardButton("📰 過去7天新聞", callback_data=f"{AI_CALLBACK_PREFIX}news_action:7d")],
        [InlineKeyboardButton("🔄 搜尋並更新新聞", callback_data=f"{AI_CALLBACK_PREFIX}news_action:refresh")],
    ])


def _compose_news_command(model: str, action: str) -> str:
    """Compose /news command with model and action."""
    parts = ["/news", action]
    if action == "refresh" and model != "gemini":
        parts.extend(["--model", model])
    return " ".join(parts)


def _compose_direct_theme_command(command: str, theme: str) -> str:
    query = " ".join(str(theme or "").strip().split())
    if command not in {"theme", "theme_flow"}:
        raise ValueError(f"Unsupported theme command: {command}")
    return f"/{command} {query}"


def _extract_first_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s<>]+", text or "")
    if not match:
        return None
    return match.group(0).rstrip(").,，。]")


def _topic_model_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Gemini", callback_data=f"{AI_CALLBACK_PREFIX}topic_maintain:model:gemini"), InlineKeyboardButton("DeepSeek V4 Pro（OpenCode Go）", callback_data=f"{AI_CALLBACK_PREFIX}topic_maintain:model:deepseek")],
        [InlineKeyboardButton("MiniMax M3", callback_data=f"{AI_CALLBACK_PREFIX}topic_maintain:model:minimax")],
    ])


def _topic_import_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("建立變更包", callback_data=f"{AI_CALLBACK_PREFIX}topic_import:confirm"),
            InlineKeyboardButton("取消", callback_data=f"{AI_CALLBACK_PREFIX}topic_import:cancel"),
        ],
    ])


def _topic_import_confirm_text(payload: str, *, from_file: bool = False) -> str:
    action_count = len(re.findall(r'"action_type"\s*:', payload or ""))
    source = "檔案內容" if from_file else "貼上的內容"
    count_text = f"偵測到約 {action_count} 筆 actions。" if action_count else "尚未偵測到 action_type，系統仍會在建立時解析 JSON。"
    return (
        f"已讀取{source}。\n"
        f"{count_text}\n\n"
        "是否建立題材變更包？\n"
        "此步驟只做本地 JSON 匯入與欄位正規化，不會呼叫 AI。"
    )


def _topic_action_keyboard(change_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("確認套用", callback_data=f"{AI_CALLBACK_PREFIX}topic_action:confirm:{change_id}"),
            InlineKeyboardButton("拒絕", callback_data=f"{AI_CALLBACK_PREFIX}topic_action:reject:{change_id}"),
        ],
        [InlineKeyboardButton("重新查看", callback_data=f"{AI_CALLBACK_PREFIX}topic_action:review:{change_id}")],
    ])


def _extract_topic_change_id(text: str) -> str | None:
    match = re.search(r"\b(change_\d{8}_\d{6}(?:_[a-z0-9]+)*)\b", text or "")
    return match.group(1) if match else None


def _topic_action_keyboard_for_text(text: str) -> InlineKeyboardMarkup | None:
    change_id = _extract_topic_change_id(text)
    if not change_id:
        return None
    if "/topic_confirm" not in text and "/topic_reject" not in text and "/topic_review" not in text:
        return None
    return _topic_action_keyboard(change_id)


def _is_topic_result(result) -> bool:
    command = getattr(getattr(result, "request", None), "command", "")
    return str(command).startswith("topic_")


def build_topic_result_message(result) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build Telegram text and buttons for topic command results."""
    summary_text = f"{result.summary}{_telegram_status_note(result)}"
    return summary_text, _topic_action_keyboard_for_text(summary_text)


async def send_topic_result_to_chat(bot, chat_id, result, send_bot_message) -> None:
    """Send a topic result to a chat using the same text/buttons as manual commands."""
    summary_text, reply_markup = build_topic_result_message(result)
    await send_bot_message(bot, chat_id, summary_text, reply_markup=reply_markup)


async def send_topic_result_reply(update: Update, result, safe_send_reply) -> None:
    """Send a topic result as a reply using the same text/buttons as scheduled jobs."""
    summary_text, reply_markup = build_topic_result_message(result)
    await safe_send_reply(update, summary_text, reply_markup=reply_markup)


def _compose_topic_maintain_command(state: dict) -> str:
    model = state.get("model") or "gemini"

    parts = ["/topic_maintain"]
    if model != "gemini":
        parts.extend(["--model", model])

    return " ".join(parts)


def _compose_menu_command(state: dict) -> str:
    command = state.get("command")
    date_flag = f" --date {state['date']}" if state.get("date") else ""
    mode = state.get("mode") or "normal"
    mode_flag = " --deep" if mode == "deep" else " --score" if mode == "score" else " --source-only" if mode == "source_only" else ""
    model_flag = f" --model {state.get('model') or 'gemini'}"
    default_market = "全球"
    default_value_source = "精選選股"
    if command == "research":
        return f"/research {state.get('target')}{mode_flag}{date_flag}{model_flag}"
    if command == "macro":
        return f"/macro {state.get('market_scope') or default_market}{mode_flag}{date_flag}{model_flag}"
    if command == "theme":
        return f"/theme {state.get('theme')}{mode_flag}{date_flag}{model_flag}"
    if command == "theme_radar":
        return f"/theme_radar{date_flag}{model_flag}"
    if command == "theme_flow":
        return f"/theme_flow {state.get('theme')}{date_flag}{model_flag}"
    if command == "sector_strength":
        return f"/sector_strength{date_flag}{model_flag}"
    if command == "value_scan":
        # 不再附加 --top，回歸資料服務層的內建限制（一般 10、deep 30）
        return f"/value_scan {state.get('source') or default_value_source}{mode_flag}{date_flag}{model_flag}"
    return "/report"


def _value_source_label(source: str) -> str:
    return {
        "curated": "精選選股",
        "all": "全市場初篩",
        "portfolio": "我的持股",
        "radar": "選股雷達",
        "monitor": "監控清單",
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


async def _send_runtime_document(update: Update, result, safe_reply_document) -> None:
    doc = (result.runtime_context or {}).get("telegram_document") or {}
    text = str(doc.get("text") or "")
    if not text:
        return
    filename = str(doc.get("filename") or "topic_seed_prompt.txt")
    caption = str(doc.get("caption") or "TXT")
    payload = io.BytesIO(text.encode("utf-8-sig"))
    await safe_reply_document(update, payload, filename, caption)


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
        await safe_send_reply(update, f"⚠️ MiniMax-M3 比較報告產出失敗。\n原因：{exc}")
        return
    model = entry.get("model") or "MiniMax-M3"
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
    ai_used = bool(getattr(result, "ai_used", False))
    ai_model = getattr(result, "ai_model", None)
    request = getattr(result, "request", None)
    source_only = bool(getattr(request, "source_only", False))
    fallback_reason = getattr(result, "fallback_reason", None)
    if ai_used and ai_model:
        notes.append(f"AI 模型：{ai_model}")
    elif source_only:
        notes.append("AI 模型：未調用（source-only 模式）")
    elif fallback_reason:
        notes.append(f"AI 模型：{_fallback_model_label(result)} 調用失敗，已改用本地 fallback")
    if fallback_reason:
        notes.append(
            f"⚠️ {_fallback_model_label(result)} 模型調用或公開來源整合未完整成功。"
            f"這不是正式 AI 完成報告，而是本地資料 fallback 報告。原因：{fallback_reason}"
        )
    return "\n\n" + "\n".join(notes) if notes else ""


def _fallback_model_label(result) -> str:
    model = (
        getattr(getattr(result, "request", None), "ai_model", None)
        or (getattr(result, "report_json", {}) or {}).get("metadata", {}).get("analysis_model_choice")
        or getattr(result, "ai_model", None)
        or "AI"
    )
    labels = {
        "gemini": "Gemini",
        "deepseek": "DeepSeek",
        "minimax": "MiniMax",
    }
    return labels.get(str(model).lower(), str(model))


def _print_progress(raw_text: str, message: str) -> None:
    from progress_logger import has_leading_timestamp, now_timestamp
    timestamp = now_timestamp()
    command = raw_text.splitlines()[0][:120] if raw_text else "AI投研"
    # If message already has a leading timestamp, don't add another one
    if has_leading_timestamp(message):
        print(f"{message}", flush=True)
    else:
        print(f"[{timestamp}] [AI投研] {command} | {message}", flush=True)


def _command_name(raw_text: str) -> str:
    first = (raw_text or "").strip().split(maxsplit=1)[0] if (raw_text or "").strip() else ""
    return first.lstrip("/").split("@", 1)[0]


def _should_use_progress_heartbeat(raw_text: str) -> bool:
    return _command_name(raw_text) in LONG_RUNNING_COMMANDS


def _heartbeat_label(raw_text: str) -> str:
    command = _command_name(raw_text) or "ai"
    first_line = (raw_text or "").splitlines()[0].strip()
    if len(first_line) > 80:
        first_line = first_line[:77] + "..."
    return f"/{command} {first_line}".strip()









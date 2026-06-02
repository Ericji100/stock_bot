import json
import asyncio
import traceback
import telegram
import pytz
from datetime import date, datetime, time, timedelta
import threading
from typing import Awaitable, Callable
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest
import curated_scan_service
from research_center.recent_scans import save_recent_scan_result

from chip_strategies import (
    CHIP_STRATEGY_NAMES,
    STRATEGY_DEFINITIONS,
    build_chip_reports,
    get_tw_today,
    is_possible_trading_day,
    warmup_chip_data_cache,
)
from data_fetcher import StockExportError, StockNotFoundError
from export_service import build_stock_export_workbook
from market_summary import (
    MarketSummaryError,
    build_morning_market_report,
    build_noon_market_report,
    is_morning_push_window,
)
from monitor_service import (
    add_monitor_stock_to_config,
    build_monitor_list_message,
    build_monitor_scan_report,
    remove_monitor_stock_from_config,
)
from backfill_service import run_full_backfill, parse_backfill_args, format_backfill_health_summary
from research_center.telegram_handlers import build_research_handlers
from research_center.news_service import run_news_refresh, run_news_latest, run_news_7d
from research_center.news_repository import NewsRepository
from research_center.news_formatters import format_news_digest, format_news_refresh_result
from portfolio_manager import (
    PORTFOLIO_PUSH_MAX_RETRIES,
    PORTFOLIO_PUSH_RETRY_DELAY_SECONDS,
    add_portfolio_stock,
    build_portfolio_report,
    escape_markdown_v2,
    list_portfolio,
    remove_portfolio_stock,
)
from stock_chart_service import StockChartError, build_stock_chart_document, parse_stock_chart_args
from stock_scanner import (
    run_scan as run_tw_market_scan,
)
# NEW: 引入技術面選股模組
import technical_scanner as ts
from radar_service import (
    RadarRequest,
    format_radar_more,
    format_radar_report,
    parse_radar_args,
    resolve_radar_report_date,
    run_radar,
)

from progress_logger import ProgressHeartbeat, now_timestamp, has_leading_timestamp, format_cmd_message
from tmf_chart_service import TmfChartError, build_tmf_chart_report, parse_tmf_chart_args

SCAN_CALLBACK_PREFIX = "scan_strategy:"
SCAN_DATE_CALLBACK_PREFIX = "scan_date:"
RADAR_DATE_CALLBACK_PREFIX = "radar_date:"
RADAR_MODEL_CALLBACK_PREFIX = "radar_model:"
NOON_REPORT_MAX_RETRIES = 6
NOON_REPORT_RETRY_DELAY_SECONDS = 30 * 60
ACTIVE_USER_TASKS: dict[int, asyncio.Task] = {}
ScheduledTaskRunner = Callable[[], Awaitable[None]]
_SCHEDULED_TASK_QUEUE: asyncio.Queue[tuple[str, ScheduledTaskRunner]] | None = None
_SCHEDULED_TASK_WORKER: asyncio.Task | None = None
_SCHEDULED_CHIP_BACKFILL_TASKS: dict[str, asyncio.Task] = {}
BOT_COMMAND_SPECS: tuple[tuple[str, str], ...] = (
    ("start", "顯示機器人指令說明"),
    ("stop", "停止目前執行中的任務"),
    ("list_m", "列出監控清單"),
    ("add_m", "加入監控股票"),
    ("del_m", "移除監控股票"),
    ("in", "加入持股"),
    ("out", "移除持股"),
    ("my", "查看持股"),
    ("check", "執行監控掃描"),
    ("scan", "執行台股選股"),
    ("radar", "執行選股雷達"),
    ("radar_more", "查看更多雷達結果"),
    ("backfill", "完整資料回補"),
    ("export", "匯出股票資料"),
    ("morning", "產生晨報"),
    ("noon", "產生午報"),
    ("tw_market", "產生台股午報"),
    ("stock_chart", "匯出個股圖表"),
    ("tmf_chart", "匯出 TMF 圖表"),
    ("research", "AI 個股投研"),
    ("macro", "AI 總經與市場分析"),
    ("theme", "AI 題材與供應鏈分析"),
    ("value_scan", "AI 價值重估掃描"),
    ("theme_radar", "AI 市場題材雷達"),
    ("theme_flow", "AI 題材擴散路徑"),
    ("sector_strength", "AI 族群強弱排行"),
    ("news", "新聞查詢與更新"),
    ("news_detail", "新聞詳情"),
    ("news_save", "保存新聞連結"),
    ("data_status", "AI 資料覆蓋狀態"),
    ("backfill_status", "回補快取健康度"),
    ("news_status", "新聞庫保存狀態"),
    ("report", "查詢 AI 投研報告"),
    ("help", "完整指令說明"),
    ("topic_maintain", "AI 題材庫維護"),
    ("topic_review", "查看題材庫變更"),
    ("topic_confirm", "確認題材庫變更"),
    ("topic_reject", "拒絕題材庫變更"),
    ("topic_profiles", "查看題材設定檔"),
    ("topic_reset", "重置題材維護狀態"),
    ("topic_seed_prompt", "產生題材種子提示詞"),
    ("topic_import", "匯入題材資料"),
    ("topic_source_sync", "同步外部產業來源"),
)
SCAN_MENU_TEXT = (
    "請選擇選股掃描策略：\n"
    "1. 財報營收選股\n"
    "2. 60 日法人動態選股\n"
    "3. 投信認養股\n"
    "4. 法人持股比例增加\n"
    "5. 每週大戶持股選股\n"
    "6. 技術面選股\n"
    "7. 全部執行"
    "\n8. 精選選股"
)
SCAN_SELECTIONS = {
    "1": ["financial"],
    "2": ["chip_1"],
    "3": ["chip_2"],
    "4": ["chip_3"],
    "5": ["chip_4"],
    "6": ["technical"],
    "7": ["financial", "chip_1", "chip_2", "chip_3", "chip_4", "technical", "curated"],
    "8": ["curated"],
}
SCAN_MENU_LABELS = {
    "1": STRATEGY_DEFINITIONS["financial"]["menu"],
    "2": STRATEGY_DEFINITIONS["chip_1"]["menu"],
    "3": STRATEGY_DEFINITIONS["chip_2"]["menu"],
    "4": STRATEGY_DEFINITIONS["chip_3"]["menu"],
    "5": STRATEGY_DEFINITIONS["chip_4"]["menu"],
    "6": "技術面選股",
    "7": STRATEGY_DEFINITIONS["all"]["menu"],
    "8": "精選選股",
}

# --- 1. 檔案管理 ---
def load_config():
    with open('config.json', 'r', encoding='utf-8') as f:
        return json.load(f)

def save_config(config):
    with open('config.json', 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)


# --- 4. 穩定發送機制 ---
async def enqueue_scheduled_task(
    context: ContextTypes.DEFAULT_TYPE,
    label: str,
    runner: ScheduledTaskRunner,
) -> None:
    """Queue scheduled report/cache jobs so they run one at a time."""
    global _SCHEDULED_TASK_QUEUE, _SCHEDULED_TASK_WORKER
    if _SCHEDULED_TASK_QUEUE is None:
        _SCHEDULED_TASK_QUEUE = asyncio.Queue()
    ahead = _SCHEDULED_TASK_QUEUE.qsize()
    await _SCHEDULED_TASK_QUEUE.put((label, runner))
    if ahead:
        print(format_cmd_message(f"{label} 已排隊，目前前方 {ahead} 個任務", "定時任務"), flush=True)
    else:
        print(format_cmd_message(f"{label} 已排入定時任務佇列", "定時任務"), flush=True)
    if _SCHEDULED_TASK_WORKER is None or _SCHEDULED_TASK_WORKER.done():
        _SCHEDULED_TASK_WORKER = context.application.create_task(_scheduled_task_worker())
        _SCHEDULED_TASK_WORKER.set_name("定時任務序列佇列")


async def _scheduled_task_worker() -> None:
    global _SCHEDULED_TASK_QUEUE
    if _SCHEDULED_TASK_QUEUE is None:
        return
    while True:
        label, runner = await _SCHEDULED_TASK_QUEUE.get()
        try:
            print(format_cmd_message(f"{label} 開始", "定時任務"), flush=True)
            await runner()
            print(format_cmd_message(f"{label} 完成", "定時任務"), flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(format_cmd_message(f"{label} 失敗：{exc}", "定時任務"), flush=True)
        finally:
            _SCHEDULED_TASK_QUEUE.task_done()


def split_telegram_message(text: str, limit: int = 4000) -> list[str]:
    chunks = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    return chunks or [text]


def append_data_footer(text: str, data_date: str, source: str) -> str:
    if "資料日期：" in text and "資料來源：" in text:
        return text
    return f"{text.rstrip()}\n\n資料日期：{data_date}\n資料來源：{source}"


async def safe_send_bot_message(bot, chat_id, text: str, **kwargs):
    for chunk in split_telegram_message(text):
        sent = False
        for i in range(3):
            try:
                await bot.send_message(chat_id=chat_id, text=chunk, **kwargs)
                sent = True
                break
            except Exception as e:
                print(f"[{now_timestamp()}] ⚠️ 排程訊息第 {i+1} 次發送失敗: {e}")
                await asyncio.sleep(3)
        if not sent:
            raise RuntimeError("排程訊息發送失敗")


async def safe_send_reply(update: Update, text: str, reply_markup=None):
    """具備自動重試的發送函式"""
    message = update.effective_message
    if message is None and update.callback_query is not None:
        message = update.callback_query.message
    if message is None:
        raise ValueError("找不到可回覆的 Telegram message")

    for chunk in split_telegram_message(text):
        html_mode = "<a href=" in chunk or "<i>" in chunk or "<b>" in chunk
        sent = False
        for i in range(3):
            try:
                kwargs = {"reply_markup": reply_markup if not sent else None}
                if html_mode:
                    kwargs.update({"parse_mode": "HTML", "disable_web_page_preview": True})
                await message.reply_text(chunk, **kwargs)
                sent = True
                reply_markup = None
                break
            except Exception as e:
                if html_mode:
                    try:
                        await message.reply_text(chunk, reply_markup=reply_markup if not sent else None)
                        sent = True
                        reply_markup = None
                        break
                    except Exception as fallback_exc:
                        print(f"[{now_timestamp()}] ⚠️ HTML 訊息回退失敗: {fallback_exc}")
                print(f"[{now_timestamp()}] ⚠️ 第 {i+1} 次發送失敗: {e}")
                await asyncio.sleep(3)
        if not sent:
            print(f"[{now_timestamp()}] ❌ 訊息發送失敗，已放棄本段回覆")
            return


async def safe_reply_document(update: Update, document, filename: str, caption: str, retries: int = 3):
    message = update.effective_message
    if message is None and update.callback_query is not None:
        message = update.callback_query.message
    if message is None:
        raise ValueError("找不到可回覆的 Telegram message")

    for attempt in range(retries):
        try:
            if hasattr(document, "seek"):
                document.seek(0)
            await message.reply_document(
                document=telegram.InputFile(document, filename=filename),
                caption=caption,
                connect_timeout=90,
                read_timeout=180,
                write_timeout=180,
                pool_timeout=90,
            )
            return
        except (telegram.error.TimedOut, telegram.error.NetworkError) as exc:
            print(f"[{now_timestamp()}] ⚠️ 檔案上傳第 {attempt + 1} 次逾時/網路失敗: {exc}")
            if attempt == retries - 1:
                raise
            await asyncio.sleep(5)


def get_update_chat_id(update: Update) -> int | None:
    chat = update.effective_chat
    if chat is None:
        return None
    return chat.id


async def run_stoppable_command(update: Update, label: str, worker):
    chat_id = get_update_chat_id(update)
    if chat_id is None:
        await worker()
        return

    existing_task = ACTIVE_USER_TASKS.get(chat_id)
    current_task = asyncio.current_task()
    if existing_task and not existing_task.done() and existing_task is not current_task:
        await safe_send_reply(update, f"目前已有任務執行中：{existing_task.get_name()}。\n如需停止，請輸入 /stop")
        return

    if current_task:
        current_task.set_name(label)
        ACTIVE_USER_TASKS[chat_id] = current_task

    try:
        await worker()
    except asyncio.CancelledError:
        print(f"[{now_timestamp()}] ⏹️ 使用者已停止任務：{label}")
    finally:
        if ACTIVE_USER_TASKS.get(chat_id) is current_task:
            ACTIVE_USER_TASKS.pop(chat_id, None)


# Per-user stop events for manual backfill
_USER_BACKFILL_STOP_EVENTS: dict[int, threading.Event] = {}

# Global stop event for scheduled backfill (set by /stop)
_SCHEDULED_BACKFILL_STOP_EVENT = threading.Event()

# Flag indicating scheduled backfill is currently running
_SCHEDULED_BACKFILL_RUNNING = False

# Background backfill tasks.  Manual /backfill is deliberately not kept in
# ACTIVE_USER_TASKS so other Telegram commands can keep running.
_USER_BACKFILL_TASKS: dict[int, asyncio.Task] = {}
_SCHEDULED_BACKFILL_TASK: asyncio.Task | None = None


async def stop_running_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_update_chat_id(update)
    if chat_id is None:
        return

    # Trigger stop event for user's backfill if running
    stop_event = _USER_BACKFILL_STOP_EVENTS.get(chat_id)
    if stop_event and not stop_event.is_set():
        stop_event.set()
        await safe_send_reply(update, "已送出停止指令：完整資料回補\n若任務正在等待外部資料來源，背景執行緒可能需要一點時間才會結束。")
        return

    # Also trigger scheduled backfill stop only if it is currently running
    if _SCHEDULED_BACKFILL_RUNNING and not _SCHEDULED_BACKFILL_STOP_EVENT.is_set():
        _SCHEDULED_BACKFILL_STOP_EVENT.set()
        await safe_send_reply(update, "已送出停止指令：定時回補\n若任務正在等待外部資料來源，背景執行緒可能需要一點時間才會結束。")
        return

    task = ACTIVE_USER_TASKS.get(chat_id)
    if task is None or task.done():
        ACTIVE_USER_TASKS.pop(chat_id, None)
        await safe_send_reply(update, "目前沒有正在執行中的任務。")
        return

    task.cancel()
    await safe_send_reply(update, f"已送出停止指令：{task.get_name()}\n若任務正在等待外部資料來源，背景執行緒可能需要一點時間才會結束。")


def make_stoppable_handler(label: str, handler):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await run_stoppable_command(update, label, lambda: handler(update, context))

    return wrapped

# --- 5. 指令處理器 ---
START_TEXT = """台股 AI 助理

選股與雷達
/scan - 選股掃描選單
/radar - 今日選股雷達
/radar_more - 查看最近 Radar 完整名單

個股與價值分析
/research - 個股研究
/value_scan - 價值重估掃描

市場與新聞
/news - 新聞選單
/macro - 宏觀研究

題材與族群
/theme - 題材研究
/theme_radar - 市場題材雷達
/sector_strength - 族群強弱排行

題材庫維護
/topic_maintain - 大範圍更新題材庫
/topic_seed_prompt - 產生外部 AI 題材庫提示詞
/topic_import - 匯入外部 AI 題材 JSON
/topic_source_sync - 同步外部產業來源快取
/topic_review - 查看題材變更包
/topic_profiles - 查看正式題材庫

持股與監控
/my - 查看我的持股
/list_m - 查看監控清單
/check - 執行監控掃描

資料回補與匯出
/backfill - 回補本地資料

報告與系統
/report - 查看報告
/morning - 晨報
/noon - 午報
/tw_market - 台股午報
/help - 完整指令說明
/stop - 停止目前任務
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send_reply(update, START_TEXT)

async def list_stocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    await safe_send_reply(update, build_monitor_list_message(config))

async def add_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    changed, message = add_monitor_stock_to_config(config, context.args)
    if changed:
        save_config(config)
    await safe_send_reply(update, message)

async def del_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    changed, message = remove_monitor_stock_from_config(config, context.args)
    if changed:
        save_config(config)
    await safe_send_reply(update, message)


# 新增監控指令別名：將策略監控與個人庫存管理分流，避免 /in、/out 與既有 /add、/del 混用。
async def list_monitor_stocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await list_stocks(update, context)


# 新增監控指令別名：策略監控正式改用 /add_m。
async def add_monitor_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await add_stock(update, context)


# 新增監控指令別名：策略監控正式改用 /del_m。
async def del_monitor_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await del_stock(update, context)


# 新增個人庫存指令：寫入獨立的 portfolio.json，不與 monitor_stocks 共用設定來源。
async def add_portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await safe_send_reply(update, "請輸入股票代號或名稱，例如 /in 2330 或 /in 台積電")
        return

    status, stock = await asyncio.to_thread(add_portfolio_stock, " ".join(context.args))
    if status == "invalid":
        await safe_send_reply(update, "❌ 查無此股票，請確認輸入正確的台股代號或名稱。")
        return

    if stock is None:
        await safe_send_reply(update, "❌ 查無此股票，請確認輸入正確的台股代號或名稱。")
        return

    if status == "exists":
        await safe_send_reply(update, f"⚠️ {stock.code} {stock.name} 已在您的庫存清單中。")
        return

    await safe_send_reply(update, f"✅ 已加入庫存：{stock.code} {stock.name}")


# 新增個人庫存指令：支援用代號或股名移除，並優先以現有 portfolio.json 比對。
async def remove_portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await safe_send_reply(update, "請輸入股票代號或名稱，例如 /out 2330 或 /out 台積電")
        return

    query = " ".join(context.args).strip()
    status, stock = await asyncio.to_thread(remove_portfolio_stock, query)
    if status == "missing":
        await safe_send_reply(update, f"⚠️ 庫存內找不到 {query}，請確認代號或名稱。")
        return

    if stock is None:
        await safe_send_reply(update, f"⚠️ 庫存內找不到 {query}，請確認代號或名稱。")
        return

    await safe_send_reply(update, f"🗑️ 已從庫存移除：{stock.code} {stock.name}")


# 新增個人庫存指令：獨立列出 portfolio.json 內容，不與策略監控清單混用。
async def list_portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    portfolio = await asyncio.to_thread(list_portfolio)
    if not portfolio:
        await safe_send_reply(update, "💼 目前您的庫存清單為空。")
        return

    lines = [f"{stock.code} {stock.name}" for stock in portfolio]
    await safe_send_reply(update, "💼 目前庫存股票：\n" + "\n".join(lines))


# 新增晨報指令：由 market_summary.py 統一整理美股四大指數與台指期夜盤資料。
async def morning_market_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send_reply(update, "🌅 正在整理晨間市場資料，請稍候...")

    try:
        report = await asyncio.to_thread(build_morning_market_report)
    except MarketSummaryError as exc:
        await safe_send_reply(update, f"❌ {exc}")
        return
    except Exception as exc:
        print(f"[{now_timestamp()}] ❌ /morning 執行失敗: {exc}")
        await safe_send_reply(update, "❌ 晨間市場資料整理失敗，請稍後再試。")
        return

    await safe_send_reply(update, report)


# 新增午報指令：由 market_summary.py 統一整理台股現貨與台指期日盤收盤資料。
async def noon_market_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send_reply(update, "📊 正在整理台股收盤資料，請稍候...")

    try:
        report = await asyncio.to_thread(build_noon_market_report)
    except MarketSummaryError as exc:
        await safe_send_reply(update, f"❌ {exc}")
        return
    except Exception as exc:
        print(f"[{now_timestamp()}] ❌ /noon 執行失敗: {exc}")
        await safe_send_reply(update, "❌ 台股收盤資料整理失敗，請稍後再試。")
        return

    await safe_send_reply(update, report)

async def run_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send_reply(update, "🔍 正在執行監控掃描，請稍候...")
    config = load_config()
    msg = await asyncio.to_thread(build_monitor_scan_report, config)
    await safe_send_reply(update, msg)

def parse_scan_report_date(args: list[str] | tuple[str, ...] | None) -> date:
    if not args:
        return get_tw_today()

    raw = str(args[0]).strip()
    today = get_tw_today()
    candidates = [raw]
    if raw.isdigit() and len(raw) == 8:
        candidates.append(f"{raw[:4]}-{raw[4:6]}-{raw[6:]}")
    if "/" in raw:
        parts = raw.split("/")
        try:
            if len(parts) == 2:
                candidates.append(f"{today.year}-{int(parts[0]):02d}-{int(parts[1]):02d}")
            elif len(parts) == 3:
                candidates.append(f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}")
        except ValueError:
            pass

    parsed_any = False
    for candidate in candidates:
        try:
            parsed = datetime.strptime(candidate, "%Y-%m-%d").date()
            parsed_any = True
            if parsed > today:
                raise ValueError("日期不可晚於今天")
            return parsed
        except ValueError:
            continue
    if parsed_any:
        raise ValueError("日期不可晚於今天")
    raise ValueError("日期格式錯誤，請使用 YYYY-MM-DD、YYYY/MM/DD、YYYYMMDD 或 M/D")


def resolve_scan_latest_report_date(today: date | None = None) -> tuple[date, str]:
    """Resolve /scan latest date to the nearest possible trading day."""
    current = today or get_tw_today()
    candidate = current
    for _ in range(10):
        if is_possible_trading_day(candidate):
            if candidate == current:
                return candidate, ""
            return (
                candidate,
                f"今天 {current.isoformat()} 不是交易日，已改用最近可用交易日 {candidate.isoformat()}",
            )
        candidate -= timedelta(days=1)
    return current, "無法判斷最近交易日，暫以今天執行"


def build_scan_strategy_keyboard(report_date: date | None = None) -> InlineKeyboardMarkup:
    # 新增 /scan 的互動式按鈕選單，避免使用者每次輸入指令就直接執行所有高成本掃描。
    # 若有 report_date，callback 會包含日期，點選後直接執行、不進日期選單。
    # 若無 report_date，callback 不含日期，點選後進入日期選單。
    rows = []
    for i in range(1, 9):
        label = SCAN_MENU_LABELS[str(i)]
        if report_date:
            callback = f"{SCAN_CALLBACK_PREFIX}{i}:{report_date.isoformat()}"
        else:
            callback = f"{SCAN_CALLBACK_PREFIX}{i}"
        rows.append([InlineKeyboardButton(f"{i}. {label}", callback_data=callback)])
    return InlineKeyboardMarkup(rows)


def build_scan_date_menu(scan_mode: str) -> InlineKeyboardMarkup:
    """Build date selection menu for a given scan mode.

    - 'latest': use the most recent available date (get_tw_today()).
    - 'custom': let user input a specific date.
    """
    rows = [
        [InlineKeyboardButton("📅 最新日期", callback_data=f"{SCAN_DATE_CALLBACK_PREFIX}latest:{scan_mode}")],
        [InlineKeyboardButton("📝 指定日期", callback_data=f"{SCAN_DATE_CALLBACK_PREFIX}custom:{scan_mode}")],
    ]
    return InlineKeyboardMarkup(rows)


async def run_selected_scan_reports(update: Update, selection: str, report_date: date | None = None):
    async def send_text(text: str) -> None:
        await safe_send_reply(update, text)

    await run_selected_scan_reports_core(selection, report_date, send_text)


async def run_selected_scan_reports_core(
    selection: str,
    report_date: date | None,
    send_text: Callable[[str], Awaitable[None]],
) -> None:
    selected_keys = SCAN_SELECTIONS.get(selection)
    if not selected_keys:
        await send_text("❌ 無效的選股策略選項。")
        return

    menu_label = SCAN_MENU_LABELS.get(selection, selection)
    if report_date is None:
        target_date, date_note = resolve_scan_latest_report_date()
    else:
        target_date, date_note = report_date, ""
    report_parts: list[str] = []
    if date_note:
        print(f"[{now_timestamp()}] [scan progress][{menu_label}] date adjusted: {date_note}", flush=True)
    print(f"[{now_timestamp()}] [選股進度][{menu_label}] 0.00% 收到 /scan 選股任務，目標日期 {target_date.isoformat()}", flush=True)

    is_curated_only = selected_keys == ["curated"]
    has_curated = "curated" in selected_keys

    if is_curated_only:
        print(f"[{now_timestamp()}] [選股進度][{menu_label}] 10.00% 開始精選交叉比對", flush=True)
        await send_text("正在比對技術面、營收財報與法人大戶策略，整理重複命中的精選名單...")
        try:
            config = load_config()
            curated_result = await asyncio.to_thread(
                curated_scan_service.build_curated_scan_result,
                config.get("scan_settings", {}),
                target_date,
            )
            print(f"[{now_timestamp()}] [選股進度][{menu_label}] 90.00% 精選報告產生完成，準備傳送 Telegram", flush=True)
            save_recent_scan_result(menu_label, target_date, curated_result.report_text, curated_result.selected_codes)
            await send_text(curated_result.report_text)
            print(f"[{now_timestamp()}] [選股進度][{menu_label}] 100.00% 完成", flush=True)
        except Exception as exc:
            print(f"[{now_timestamp()}] ❌ /scan 精選選股失敗: {exc}")
            await send_text("⚠️ 精選選股產生失敗，請稍後再試。")
        return

    # 完成一段就先送一段，避免全部選股遇到單一資料源變慢時使用者長時間沒有任何回應。
    if "financial" in selected_keys:
        try:
            print(f"[{now_timestamp()}] [選股進度][{menu_label}] 10.00% 開始財報營收選股", flush=True)
            config = load_config()
            financial_report = await asyncio.to_thread(
                run_tw_market_scan,
                False,
                None,
                config.get("scan_settings", {}),
            )
            print(f"[{now_timestamp()}] [選股進度][{menu_label}] 35.00% 財報營收報告完成，準備傳送 Telegram", flush=True)
            report_parts.append(financial_report)
            await send_text(financial_report)
        except Exception as exc:
            print(f"[{now_timestamp()}] ❌ /scan 財報選股失敗: {exc}")
            await send_text("⚠️ 財報選股產生失敗，會繼續嘗試其他已選策略。")

    chip_keys = [key for key in selected_keys if key.startswith("chip_")]
    has_technical = "technical" in selected_keys
    if not chip_keys and not has_technical and not has_curated:
        print(f"[{now_timestamp()}] [選股進度][{menu_label}] 100.00% 完成", flush=True)
        return

    if chip_keys:
        chip_progress_end = 70.0 if has_technical else 90.0
        print(f"[{now_timestamp()}] [選股進度][{menu_label}] 40.00% 開始籌碼策略資料整理", flush=True)
        await send_text("籌碼選股資料整理中。")
        try:
            chip_reports, _ = await asyncio.to_thread(
                build_chip_reports,
                chip_keys,
                False,
                target_date,
                menu_label,
                40.0,
                chip_progress_end,
            )
        except Exception as exc:
            print(f"[{now_timestamp()}] ❌ /scan 籌碼選股失敗: {exc}")
            await send_text("⚠️ 籌碼選股產生失敗，請稍後再試或先單獨執行其他策略。")
            return

        for index, key in enumerate(chip_keys, start=1):
            progress = chip_progress_end + index / max(1, len(chip_keys)) * 5.0
            print(f"[{now_timestamp()}] [選股進度][{menu_label}] {progress:.2f}% 傳送 {CHIP_STRATEGY_NAMES.get(key, key)} 報告", flush=True)
            report_parts.append(chip_reports[key])
            await send_text(chip_reports[key])

    # NEW: 技術面選股路由
    if has_technical:
        technical_start_progress = 75.0 if chip_keys or "financial" in selected_keys else 10.0
        print(f"[{now_timestamp()}] [選股進度][{menu_label}] {technical_start_progress:.2f}% 開始技術面選股", flush=True)
        await send_text("技術面選股資料整理中。")
        try:
            config = load_config()
            technical_report = await asyncio.to_thread(
                ts.build_technical_scan_report,
                config.get("scan_settings", {}),
                target_date,
            )
            print(f"[{now_timestamp()}] [選股進度][{menu_label}] 98.00% 技術面報告完成，準備傳送 Telegram", flush=True)
            report_parts.append(technical_report)
            await send_text(technical_report)
        except Exception as exc:
            print(f"[{now_timestamp()}] ❌ /scan 技術面選股失敗: {exc}")
            await send_text("⚠️ 技術面選股產生失敗，請稍後再試。")

    if has_curated:
        print(f"[{now_timestamp()}] [選股進度][{menu_label}] 99.00% 開始精選交叉比對", flush=True)
        await send_text("正在比對技術面、營收財報與法人大戶策略，整理重複命中的精選名單...")
        try:
            config = load_config()
            curated_result = await asyncio.to_thread(
                curated_scan_service.build_curated_scan_result,
                config.get("scan_settings", {}),
                target_date,
            )
            print(f"[{now_timestamp()}] [選股進度][{menu_label}] 99.50% 精選報告產生完成，準備傳送 Telegram", flush=True)
            report_parts.append(curated_result.report_text)
            await send_text(curated_result.report_text)
        except Exception as exc:
            print(f"[{now_timestamp()}] ❌ /scan 精選選股失敗: {exc}")
            await send_text("⚠️ 精選選股產生失敗，會繼續完成其他已選策略。")

    if selection == "7" and report_parts:
        save_recent_scan_result(menu_label, target_date, "\n\n".join(report_parts))

    print(f"[{now_timestamp()}] [選股進度][{menu_label}] 100.00% 完成", flush=True)


def _format_backfill_skip_reason(reason: str | None) -> str:
    return {
        "already_running": "已有回補任務正在執行",
        "cache_complete": "已有有效快取，略過",
        "market_data_unavailable": "市場資料尚不可用，略過",
        "today_before_1500": "15:00 前不抓取當日資料",
        "today_data_check_failed": "當日資料檢查失敗，略過",
        "future_date": "指定日期在未來，略過",
        "cache_technical_gaps": "技術面快取仍有缺口",
        "cache_revenue_gaps": "月營收快取仍有缺口",
        "cache_chip_gaps": "籌碼快取仍有缺口",
    }.get(reason or "", f"原因：{reason}")


async def _send_backfill_decision_message(bot, chat_id: int, decision, category: str) -> None:
    if decision.status == "skipped":
        await bot.send_message(chat_id=chat_id, text=f"⏭️ {category}略過：{_format_backfill_skip_reason(decision.reason)}")
        return

    if decision.status == "stopped":
        warning_text = ""
        if decision.result and decision.result.warnings:
            warning_text = "\n警告：\n" + "\n".join(f"- {item}" for item in decision.result.warnings[:5])
            if len(decision.result.warnings) > 5:
                warning_text += f"\n...另有 {len(decision.result.warnings) - 5} 筆，請看 CMD。"
        await bot.send_message(chat_id=chat_id, text=f"🛑 {category}已停止，未寫入完成標記。{warning_text}")
        return

    if decision.status != "completed" or not decision.result:
        await bot.send_message(chat_id=chat_id, text=f"⚠️ {category}未完成：{decision.status} / {decision.reason}")
        return

    result = decision.result
    warning_text = ""
    if result.warnings:
        warning_text = "\n警告：\n" + "\n".join(f"- {item}" for item in result.warnings[:5])
        if len(result.warnings) > 5:
            warning_text += f"\n...另有 {len(result.warnings) - 5} 筆，請看 CMD。"

    health_text = format_backfill_health_summary(result)
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ {category}完成\n"
            f"資料日期：{result.report_date.isoformat()}\n\n"
            f"股票宇宙：{result.universe_count} 檔\n"
            f"候選池：{result.candidate_count} 檔\n"
            f"核心投研：{result.core_research_count} 檔\n"
            f"投研結構化成功：{result.research_structured_count} 檔\n"
            f"逾時跳過：{result.research_structured_timeout_count} 檔\n\n"
            f"{health_text}\n\n{warning_text}"
        ),
    )


async def _manual_backfill_background(
    chat_id: int,
    bot,
    report_date_arg: date | None,
    force_refresh: bool,
    stop_event: threading.Event,
) -> None:
    heartbeat = ProgressHeartbeat(
        "/backfill",
        sink=lambda message: print(format_cmd_message(message, "完整回補"), flush=True),
    ).start()

    def progress(message: str) -> None:
        heartbeat.update(message)
        print(format_cmd_message(message, "完整回補"), flush=True)

    try:
        from backfill_service import run_backfill_if_needed

        decision = await asyncio.to_thread(
            run_backfill_if_needed,
            report_date_arg,
            force_refresh,
            progress,
            stop_event,
        )
        await _send_backfill_decision_message(bot, chat_id, decision, "完整資料回補")
    except Exception as exc:
        print(format_cmd_message(f"背景完整回補失敗：{exc}", "完整回補"), flush=True)
        await bot.send_message(chat_id=chat_id, text=f"❌ 完整資料回補失敗：{exc}")
    finally:
        heartbeat.stop()
        task = asyncio.current_task()
        if _USER_BACKFILL_TASKS.get(chat_id) is task:
            _USER_BACKFILL_TASKS.pop(chat_id, None)
        if _USER_BACKFILL_STOP_EVENTS.get(chat_id) is stop_event:
            _USER_BACKFILL_STOP_EVENTS.pop(chat_id, None)


async def _scheduled_backfill_background() -> None:
    global _SCHEDULED_BACKFILL_RUNNING, _SCHEDULED_BACKFILL_TASK
    heartbeat = ProgressHeartbeat(
        "定時回補檢查",
        sink=lambda message: print(format_cmd_message(message, "定時回補檢查"), flush=True),
    ).start()

    def progress(message: str) -> None:
        heartbeat.update(message)
        print(format_cmd_message(message, "定時回補檢查"), flush=True)

    try:
        from backfill_service import run_backfill_if_needed

        decision = await asyncio.to_thread(
            run_backfill_if_needed,
            None,
            False,
            progress,
            _SCHEDULED_BACKFILL_STOP_EVENT,
        )

        if decision.status == "skipped":
            print(
                format_cmd_message(
                    f"跳過 - {_format_backfill_skip_reason(decision.reason)}",
                    "定時回補檢查",
                ),
                flush=True,
            )
            return

        if decision.status == "stopped":
            print(format_cmd_message("已停止，未寫入完成標記", "定時回補檢查"), flush=True)
            return

        if decision.status == "completed" and decision.result:
            result = decision.result
            print(
                format_cmd_message(
                    (
                        f"完成：宇宙 {result.universe_count} 檔，"
                        f"候選 {result.candidate_count} 檔，"
                        f"核心 {result.core_research_count} 檔，"
                        f"投研成功 {result.research_structured_count} 檔，"
                        f"逾時 {result.research_structured_timeout_count} 檔"
                    ),
                    "定時回補檢查",
                ),
                flush=True,
            )
            try:
                health = format_backfill_health_summary(result)
                print(format_cmd_message(f"\n{health}", "定時回補檢查"), flush=True)
            except Exception:
                pass
            return

        print(
            format_cmd_message(f"未完成：{decision.status} / {decision.reason}", "定時回補檢查"),
            flush=True,
        )
    except Exception as exc:
        print(format_cmd_message(f"背景定時回補失敗：{exc}", "定時回補檢查"), flush=True)
    finally:
        heartbeat.stop()
        _SCHEDULED_BACKFILL_RUNNING = False
        _SCHEDULED_BACKFILL_TASK = None


async def manual_full_backfill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /backfill command: build candidate pool and warm up local data caches."""
    chat_id = get_update_chat_id(update)
    if chat_id is None:
        await safe_send_reply(update, "無法取得聊天室，請稍後再試。")
        return

    try:
        report_date_arg, force_refresh = parse_backfill_args(context.args or [])
    except ValueError as exc:
        await safe_send_reply(update, f"❌ {exc}")
        return

    if report_date_arg is None:
        from backfill_service import resolve_backfill_report_date
        target_date = resolve_backfill_report_date()
    else:
        target_date = report_date_arg

    existing_task = _USER_BACKFILL_TASKS.get(chat_id)
    if existing_task and not existing_task.done():
        await safe_send_reply(
            update,
            "完整資料回補已在背景執行中。\n"
            "你可以繼續使用其他指令；若要停止回補，請輸入 /stop。",
        )
        return

    await safe_send_reply(
        update,
        (
            "已啟動背景完整資料回補。\n"
            f"資料日期：{target_date.isoformat()}\n"
            f"強制刷新：{'是' if force_refresh else '否'}\n"
            "你可以繼續使用其他 Telegram 指令。\n"
            "若要停止背景回補，請輸入 /stop。\n"
            "回補完成後會自動補發結果訊息。"
        ),
    )

    stop_event = threading.Event()
    _USER_BACKFILL_STOP_EVENTS[chat_id] = stop_event
    task = context.application.create_task(
        _manual_backfill_background(chat_id, context.bot, report_date_arg, force_refresh, stop_event)
    )
    task.set_name("完整資料回補")
    _USER_BACKFILL_TASKS[chat_id] = task
    return

    chat_id = get_update_chat_id(update)
    if chat_id is None:
        await safe_send_reply(update, "無法確定使用者，請重新嘗試。")
        return

    try:
        report_date_arg, force_refresh = parse_backfill_args(context.args or [])
    except ValueError as exc:
        await safe_send_reply(update, f"❌ {exc}")
        return

    # Determine target date for the reply message (resolve if None)
    if report_date_arg is None:
        from backfill_service import resolve_backfill_report_date
        target_date = resolve_backfill_report_date()
    else:
        target_date = report_date_arg

    await safe_send_reply(
        update,
        (
            "已收到完整資料回補任務。\n"
            f"資料日期：{target_date.isoformat()}\n"
            f"強制刷新：{'是' if force_refresh else '否'}\n"
            "會先回補全市場硬篩基礎資料，再建立候選池並回補完整資料；"
            "AI 搜尋與模型分析仍於投研指令執行時即時取得。"
        ),
    )

    # Create per-user stop event
    stop_event = threading.Event()
    _USER_BACKFILL_STOP_EVENTS[chat_id] = stop_event

    def progress(message: str) -> None:
        print(format_cmd_message(message, "完整回補"), flush=True)

    try:
        from backfill_service import run_backfill_if_needed

        decision = await asyncio.to_thread(
            run_backfill_if_needed,
            report_date_arg,
            force_refresh,
            progress,
            stop_event,
        )

        if decision.status == "skipped":
            reason_text = {
                "already_running": "已有回補執行中。",
                "cache_complete": "已有快取，略過。",
                "market_data_unavailable": "資料尚未發布，略過。",
                "today_before_1500": "15:00 前，略過。",
                "today_data_check_failed": "今日資料檢查失敗，略過。",
                "future_date": "未來日期，略過。",
            }.get(decision.reason or "", f"原因：{decision.reason}")
            await safe_send_reply(update, f"ℹ️ {reason_text}")
            return

        if decision.status == "stopped":
            warning_text = ""
            if decision.result and decision.result.warnings:
                warning_text = "\n警告：" + "\n".join(f"- {item}" for item in decision.result.warnings[:5])
                if len(decision.result.warnings) > 5:
                    warning_text += f"\n...另有 {len(decision.result.warnings) - 5} 筆警告，請看 CMD。"
            await safe_send_reply(
                update,
                f"🛑 完整資料回補已停止，未寫入完成標記。{warning_text}",
            )
            return

        result = decision.result

        warning_text = ""
        if result.warnings:
            warning_text = "\n警告：" + "\n".join(f"- {item}" for item in result.warnings[:5])
            if len(result.warnings) > 5:
                warning_text += f"\n...另有 {len(result.warnings) - 5} 筆警告，請看 CMD。"
        health_text = format_backfill_health_summary(result)
        await safe_send_reply(
            update,
            (
                "✅ 完整資料回補完成。\n"
                f"資料日期：{result.report_date.isoformat()}\n\n"
                "【全市場輕量回補】\n"
                f"- 股票宇宙：{result.universe_count} 檔\n"
                f"- 月營收涵蓋：{result.screening_revenue_count} 檔\n"
                f"- 價量資料涵蓋：{result.screening_price_metric_count} 檔\n"
                f"- 技術日線快取：{result.screening_technical_count} 檔\n\n"
                "【候選股中量回補】\n"
                f"- 候選池：{result.candidate_count} 檔\n"
                f"- 毛利率：{result.gross_margin_count} 檔\n"
                f"- 籌碼資料：{result.chip_candidate_count} 檔\n"
                f"- 精選選股：{result.curated_scan_count} 檔\n\n"
                "【核心股完整投研回補】\n"
                f"- 核心股：{result.core_research_count} 檔\n"
                f"- 完整投研成功：{result.research_structured_count} 檔\n"
                f"- 快取命中：{len(result.used_cache)} 檔\n"
                f"- 逾時跳過：{result.research_structured_timeout_count} 檔\n\n"
                f"{health_text}\n\n{warning_text}"
            ),
        )
    except Exception as exc:
        print(f"[{now_timestamp()}] ❌ [完整回補] 失敗：{exc}", flush=True)
        await safe_send_reply(update, f"❌ 完整資料回補失敗：{exc}")
    finally:
        _USER_BACKFILL_STOP_EVENTS.pop(chat_id, None)


async def run_tw_stock_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 新增 /scan 互動流程：先讓使用者選策略，再由 callback handler 執行對應掃描。
    # /scan 無參數時 report_date 為 None，進入「選擇策略 → 日期選單」流程。
    # /scan <日期> 時 report_date 有值，進入「選擇策略 → 直接執行」流程。
    try:
        report_date = parse_scan_report_date(context.args) if context.args else None
    except ValueError as exc:
        await safe_send_reply(update, f"❌ {exc}\n範例：/scan 2026-05-05")
        return

    if report_date:
        text = f"{SCAN_MENU_TEXT}\n\n目標資料日期：{report_date.isoformat()}"
    else:
        text = SCAN_MENU_TEXT
    await safe_send_reply(update, text, reply_markup=build_scan_strategy_keyboard(report_date))


async def handle_scan_strategy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    payload = query.data.replace(SCAN_CALLBACK_PREFIX, "", 1)
    parts = payload.split(":", 1)
    selection = parts[0]
    date_str = parts[1] if len(parts) > 1 else None

    if selection not in SCAN_SELECTIONS:
        await query.edit_message_text("❌ 無效的選股策略選項。")
        return

    if date_str:
        # /scan <date> 舊用法：callback 帶日期，直接執行
        try:
            report_date = parse_scan_report_date([date_str])
        except ValueError:
            await query.edit_message_text("❌ 日期格式錯誤。")
            return
        menu_label = SCAN_MENU_LABELS[selection]
        await query.edit_message_text(
            f"已選擇：{menu_label}\n目標資料日期：{report_date.isoformat()}\n開始執行，請稍候...",
        )
        await run_stoppable_command(
            update,
            f"選股：{menu_label}",
            lambda: run_selected_scan_reports(update, selection, report_date),
        )
    else:
        # /scan 不帶日期：進入日期選單
        menu_label = SCAN_MENU_LABELS[selection]
        await query.edit_message_text(
            f"已選擇：{menu_label}\n請選擇資料日期：",
            reply_markup=build_scan_date_menu(selection),
        )


async def handle_scan_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle date selection callback for /scan.

    Sets user state so that any text message received next is treated as
    a date input for the pending scan mode.
    """
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    payload = query.data.replace(SCAN_DATE_CALLBACK_PREFIX, "", 1)
    parts = payload.split(":", 1)
    date_action = parts[0]
    scan_mode = parts[1] if len(parts) > 1 else None

    if scan_mode is None or scan_mode not in SCAN_SELECTIONS:
        await query.edit_message_text("❌ 無效的選股策略。")
        return

    if date_action == "latest":
        # Execute immediately with today's date
        menu_label = SCAN_MENU_LABELS[scan_mode]
        report_date, date_note = resolve_scan_latest_report_date()
        note = f"\n{date_note}" if date_note else ""
        await query.edit_message_text(
            f"已選擇：{menu_label}\n目標資料日期：{report_date.isoformat()}{note}\n開始執行，請稍候..."
        )
        await run_stoppable_command(
            update,
            f"選股：{menu_label}",
            lambda: run_selected_scan_reports(update, scan_mode, report_date),
        )
    elif date_action == "custom":
        # Store pending state and ask user for date
        context.user_data["awaiting_scan_date"] = True
        context.user_data["pending_scan_mode"] = scan_mode
        menu_label = SCAN_MENU_LABELS[scan_mode]
        await query.edit_message_text(
            f"已選擇：{menu_label}\n請輸入日期，格式 YYYY-MM-DD 或 YYYY/MM/DD",
        )
    else:
        await query.edit_message_text("❌ 無效的日期選項。")


async def handle_scan_date_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle arbitrary text input when user is in 'awaiting_scan_date' state.

    Parses the input as a date and executes the pending scan if valid.
    """
    ai_menu_state = context.user_data.get("ai_menu") or {}
    if ai_menu_state.get("awaiting"):
        return

    if context.user_data.get("awaiting_radar_date"):
        await handle_radar_date_text_input(update, context)
        return

    if not context.user_data.get("awaiting_scan_date"):
        return  # Not in date-input state; let other handlers process

    scan_mode = context.user_data.get("pending_scan_mode")
    if not scan_mode or scan_mode not in SCAN_SELECTIONS:
        context.user_data.pop("awaiting_scan_date", None)
        context.user_data.pop("pending_scan_mode", None)
        await safe_send_reply(update, "❌ 選股模式無效，請重新輸入 /scan。")
        return

    raw = update.message.text.strip()

    # Try to parse date using the same logic as parse_scan_report_date
    try:
        report_date = parse_scan_report_date([raw])
    except ValueError:
        await safe_send_reply(
            update,
            "❌ 日期格式錯誤，請使用 YYYY-MM-DD 或 YYYY/MM/DD 或 YYYYMMDD。",
        )
        return

    # Clear pending state
    context.user_data.pop("awaiting_scan_date", None)
    context.user_data.pop("pending_scan_mode", None)

    menu_label = SCAN_MENU_LABELS[scan_mode]
    await update.message.reply_text(
        f"已選擇：{menu_label}\n目標資料日期：{report_date.isoformat()}\n開始執行，請稍候..."
    )
    await run_stoppable_command(
        update,
        f"選股：{menu_label}",
        lambda: run_selected_scan_reports(update, scan_mode, report_date),
    )


async def run_radar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        request = parse_radar_args(context.args)
    except ValueError as exc:
        await safe_send_reply(update, f"❌ {exc}\n範例：/radar --source technical --ai-top 5 --model deepseek")
        return

    if not context.args and request.report_date is None:
        context.user_data["pending_radar_args"] = []
        latest_date, date_note = resolve_radar_report_date(None)
        note = f"\n提示：{date_note}" if date_note else ""
        await safe_send_reply(
            update,
            (
                "📡 Radar 請選擇資料日期\n"
                f"來源：{request.source}\n"
                f"最新交易日：{latest_date.isoformat()}{note}"
            ),
            reply_markup=build_radar_date_keyboard(),
        )
        return

    if request.ai_comment_enabled and not request.model:
        await prompt_radar_model_selection(update, context, request, list(context.args or []))
        return

    await execute_radar_request(update, context, request)


def build_radar_date_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📅 最新交易日", callback_data=f"{RADAR_DATE_CALLBACK_PREFIX}latest")],
            [InlineKeyboardButton("📝 指定日期", callback_data=f"{RADAR_DATE_CALLBACK_PREFIX}custom")],
        ]
    )


def build_radar_model_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Gemini", callback_data=f"{RADAR_MODEL_CALLBACK_PREFIX}gemini")],
            [InlineKeyboardButton("DeepSeek V4 Pro", callback_data=f"{RADAR_MODEL_CALLBACK_PREFIX}deepseek")],
            [InlineKeyboardButton("MiniMax M3", callback_data=f"{RADAR_MODEL_CALLBACK_PREFIX}minimax")],
            [InlineKeyboardButton("略過 AI 短評", callback_data=f"{RADAR_MODEL_CALLBACK_PREFIX}skip")],
        ]
    )


async def prompt_radar_model_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    request: RadarRequest,
    args: list[str] | None = None,
) -> None:
    context.user_data["pending_radar_args"] = list(args or [])
    date_text = request.report_date.isoformat() if request.report_date else "最新交易日"
    await safe_send_reply(
        update,
        (
            "📡 Radar 請選擇 AI 短評模型\n"
            f"來源：{request.source}\n"
            f"日期：{date_text}\n"
            f"分析範圍：每策略 Top {request.ai_top}"
        ),
        reply_markup=build_radar_model_keyboard(),
    )


async def handle_radar_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    action = query.data.replace(RADAR_DATE_CALLBACK_PREFIX, "", 1)
    args = list(context.user_data.get("pending_radar_args", []))
    try:
        request = parse_radar_args(args)
    except ValueError as exc:
        await query.edit_message_text(f"❌ {exc}\n請重新輸入 /radar。")
        return

    if action == "latest":
        latest_date, date_note = resolve_radar_report_date(None)
        request = RadarRequest(
            source=request.source,
            report_date=latest_date,
            ai_top=request.ai_top,
            model=request.model,
            ai_comment_enabled=request.ai_comment_enabled,
        )
        note = f"\n提示：{date_note}" if date_note else ""
        await query.edit_message_text(f"已選擇：最新交易日 {latest_date.isoformat()}{note}")
        if request.ai_comment_enabled and not request.model:
            await prompt_radar_model_selection(update, context, request, ["--date", latest_date.isoformat(), "--source", request.source, "--ai-top", str(request.ai_top)])
            return
        await run_stoppable_command(update, "Radar 選股雷達", lambda: execute_radar_request(update, context, request))
        return

    if action == "custom":
        context.user_data["awaiting_radar_date"] = True
        await query.edit_message_text("請輸入 Radar 日期，格式 YYYY-MM-DD 或 YYYY/MM/DD。")
        return

    await query.edit_message_text("❌ 無效的 Radar 日期選項。")


async def handle_radar_date_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_radar_date"):
        return
    raw = update.message.text.strip()
    try:
        selected_date = parse_radar_args([raw]).report_date
        if selected_date is None:
            raise ValueError("missing date")
    except ValueError:
        await safe_send_reply(update, "❌ 日期格式錯誤，請使用 YYYY-MM-DD 或 YYYY/MM/DD 或 YYYYMMDD。")
        return

    context.user_data.pop("awaiting_radar_date", None)
    args = list(context.user_data.get("pending_radar_args", []))
    try:
        request = parse_radar_args(args)
    except ValueError as exc:
        context.user_data.pop("pending_radar_args", None)
        await safe_send_reply(update, f"❌ {exc}\n請重新輸入 /radar。")
        return
    request = RadarRequest(
        source=request.source,
        report_date=selected_date,
        ai_top=request.ai_top,
        model=request.model,
        ai_comment_enabled=request.ai_comment_enabled,
    )
    await safe_send_reply(update, f"已選擇 Radar 日期：{selected_date.isoformat()}")
    if request.ai_comment_enabled and not request.model:
        await prompt_radar_model_selection(update, context, request, ["--date", selected_date.isoformat(), "--source", request.source, "--ai-top", str(request.ai_top)])
        return
    await run_stoppable_command(update, "Radar 選股雷達", lambda: execute_radar_request(update, context, request))


async def handle_radar_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    model = query.data.replace(RADAR_MODEL_CALLBACK_PREFIX, "", 1)
    args = list(context.user_data.pop("pending_radar_args", []))
    try:
        request = parse_radar_args(args)
    except ValueError as exc:
        await query.edit_message_text(f"❌ {exc}\n請重新輸入 /radar。")
        return
    if model == "skip":
        request = RadarRequest(
            source=request.source,
            report_date=request.report_date,
            ai_top=request.ai_top,
            model=None,
            ai_comment_enabled=False,
        )
        await query.edit_message_text("已選擇：略過 AI 短評\n開始執行 Radar，請稍候...")
    else:
        request = RadarRequest(
            source=request.source,
            report_date=request.report_date,
            ai_top=request.ai_top,
            model=model,
            ai_comment_enabled=True,
        )
        await query.edit_message_text(f"已選擇：{model}\n開始執行 Radar，請稍候...")

    await run_stoppable_command(update, "Radar 選股雷達", lambda: execute_radar_request(update, context, request))


async def execute_radar_request(update: Update, context: ContextTypes.DEFAULT_TYPE, request: RadarRequest):
    config = load_config()
    display_date, date_note = resolve_radar_report_date(request.report_date)
    date_label = display_date.isoformat() if request.report_date is None else request.report_date.isoformat()
    date_note_text = f"\n提示：{date_note}" if date_note else ""
    await safe_send_reply(
        update,
        (
            "📡 Radar 開始執行\n"
            f"來源：{request.source}\n"
            f"日期：{date_label}{date_note_text}\n"
            f"{_radar_start_mode_text(request)}"
        ),
    )

    heartbeat = ProgressHeartbeat(
        "/radar",
        sink=lambda message: print(f"[{now_timestamp()}] {message}", flush=True),
    ).start()
    try:
        from backfill_service import is_backfill_running

        if is_backfill_running():
            notice = "偵測到完整資料回補正在執行，本次 Radar 會優先使用既有快取與逾時降級，避免長時間等待資料源。"
            heartbeat.update(notice, stage="回補狀態檢查")
            print(f"[{now_timestamp()}] {notice}", flush=True)
    except Exception:
        pass

    def progress(message: str) -> None:
        heartbeat.update(message)
        print(f"[{now_timestamp()}] {message}", flush=True)

    try:
        result = await asyncio.to_thread(
            run_radar,
            request,
            scan_settings=config.get("scan_settings", {}),
            config=config,
            progress=progress,
        )
    except asyncio.CancelledError:
        heartbeat.stop()
        raise
    except Exception as exc:
        print(f"[{now_timestamp()}] ❌ /radar 執行失敗: {exc}", flush=True)
        print(traceback.format_exc(), flush=True)
        heartbeat.stop()
        await safe_send_reply(update, f"❌ Radar 執行失敗：{exc}")
        return
    heartbeat.stop()

    await safe_send_reply(update, format_radar_report(result))


def _radar_start_mode_text(request: RadarRequest) -> str:
    if request.ai_comment_enabled and request.model:
        label = {"gemini": "Gemini", "deepseek": "DeepSeek", "minimax": "MiniMax"}.get(request.model, request.model)
        return f"AI短評：{label}｜每策略 Top {request.ai_top}"
    if request.ai_comment_enabled:
        return f"外部來源補強：每策略 Top {request.ai_top}"
    return "AI短評：略過"


async def run_radar_more_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    report_date = None
    if context.args:
        try:
            report_date = parse_radar_args([context.args[0]]).report_date
        except ValueError as exc:
            await safe_send_reply(update, f"❌ {exc}\n範例：/radar_more 2026-05-20")
            return
    await safe_send_reply(update, format_radar_more(report_date))


async def export_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await safe_send_reply(update, "請輸入股票代碼，例如 /export 2330")
        return

    raw_symbol = context.args[0].strip()
    await safe_send_reply(update, "🔎 正在從證交所調取數據...")

    try:
        export_buffer, filename, display_name = await asyncio.to_thread(build_stock_export_workbook, raw_symbol)
    except StockNotFoundError as exc:
        await safe_send_reply(update, f"❌ {exc}")
        return
    except StockExportError as exc:
        await safe_send_reply(update, f"❌ 匯出失敗：{exc}")
        return
    except Exception as exc:
        print(f"[{now_timestamp()}] ❌ /export 執行失敗 {raw_symbol}: {exc}")
        await safe_send_reply(update, "❌ 匯出資料時發生未預期錯誤，請稍後再試。")
        return

    await update.message.reply_document(
        document=telegram.InputFile(export_buffer, filename=filename),
        caption=append_data_footer(
            f"📁 {display_name} 匯出完成",
            get_tw_today().isoformat(),
            "TWSE / TPEX / Yahoo Finance / FinMind / Fugle / 本機快取",
        ),
    )


# 新增指令：產出台股個股的互動式 Lightweight Charts HTML 報表。
async def export_stock_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chart_request = parse_stock_chart_args(context.args)
    except StockChartError as exc:
        await safe_send_reply(update, f"❌ {exc}")
        return

    await safe_send_reply(update, "📈 正在生成個股互動圖表，請稍候...")

    try:
        # 新增流程：圖表資料抓取、指標計算與 HTML 生成都封裝在獨立服務檔，main.py 僅負責 Telegram 互動。
        html_buffer, filename, meta = await asyncio.to_thread(
            build_stock_chart_document,
            chart_request.code,
            chart_request.start_date.isoformat(),
            chart_request.end_date.isoformat(),
            chart_request.frequency,
        )
    except StockChartError as exc:
        await safe_send_reply(update, f"❌ {exc}")
        return
    except Exception as exc:
        print(f"[{now_timestamp()}] ❌ /stock_chart 執行失敗: {exc}")
        await safe_send_reply(update, "❌ 生成個股圖表時發生未預期錯誤，請稍後再試。")
        return

    try:
        await safe_reply_document(
            update,
            html_buffer,
            filename,
            append_data_footer(
                (
                    f"📊 {meta.display_name} {chart_request.start_date} ~ "
                    f"{chart_request.end_date} {chart_request.frequency}"
                ),
                f"{chart_request.start_date} ~ {chart_request.end_date}",
                "TWSE / TPEX / Yahoo Finance / Fugle",
            ),
        )
    except (telegram.error.TimedOut, telegram.error.NetworkError):
        await safe_send_reply(update, "⚠️ 個股圖表已產生，但 Telegram 上傳檔案逾時，請稍後再試。")


# 新增指令：產出 TMF 的互動式 HTML 技術分析圖表。
async def export_tmf_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chart_request = parse_tmf_chart_args(context.args)
    except TmfChartError as exc:
        await safe_send_reply(update, f"❌ {exc}")
        return

    await safe_send_reply(update, "📈 正在生成 TMF 互動圖表，請稍候...")

    html_path = None
    try:
        # 新增流程：把圖表生成邏輯放到獨立服務檔，main.py 只保留參數解析與 Telegram 傳送。
        html_path = await asyncio.to_thread(
            build_tmf_chart_report,
            chart_request.start_date.isoformat(),
            chart_request.end_date.isoformat(),
            chart_request.session,
            chart_request.frequency,
        )
    except TmfChartError as exc:
        await safe_send_reply(update, f"❌ {exc}")
        return
    except Exception as exc:
        print(f"[{now_timestamp()}] ❌ /tmf_chart 執行失敗: {exc}")
        await safe_send_reply(update, "❌ 生成 TMF 圖表時發生未預期錯誤，請稍後再試。")
        return

    try:
        with html_path.open("rb") as html_file:
            await safe_reply_document(
                update,
                html_file,
                html_path.name,
                append_data_footer(
                    (
                        f"📊 TMF {chart_request.start_date} ~ {chart_request.end_date} "
                        f"{chart_request.session} {chart_request.frequency}"
                    ),
                    f"{chart_request.start_date} ~ {chart_request.end_date}",
                    "TAIFEX / 本機快取",
                ),
            )
    except (telegram.error.TimedOut, telegram.error.NetworkError):
        await safe_send_reply(update, "⚠️ TMF 圖表已產生，但 Telegram 上傳檔案逾時，請稍後再試。")
    finally:
        # 新增清理：送出完成後立即刪除暫存 HTML，避免暫存資料堆積。
        if html_path and html_path.exists():
            html_path.unlink(missing_ok=True)

# --- 6. 定時任務 ---
async def scheduled_daily_scan(context: ContextTypes.DEFAULT_TYPE):
    await enqueue_scheduled_task(context, "12:30 監控掃描", lambda: _scheduled_daily_scan(context))


async def _scheduled_daily_scan(context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    print(f"[{now_timestamp()}] 🔍 執行 12:30 監控掃描...")
    msg = await asyncio.to_thread(
        build_monitor_scan_report,
        config,
        "📌 12:30 監控突破通知",
        "目前沒有符合條件的突破訊號。",
    )
    try:
        await context.bot.send_message(chat_id=config['chat_id'], text=msg)
        print(f"[{now_timestamp()}] ✅ 12:30 監控通知已發送")
    except Exception as e:
        print(f"[{now_timestamp()}] ⚠️ 12:30 監控通知發送失敗：{e}")


async def scheduled_radar_push(context: ContextTypes.DEFAULT_TYPE):
    await enqueue_scheduled_task(context, "21:30 Radar 推播（MiniMax M3 短評）", lambda: _scheduled_radar_push(context))


async def scheduled_all_scan_push(context: ContextTypes.DEFAULT_TYPE):
    await enqueue_scheduled_task(context, "20:30 全部選股", lambda: _scheduled_all_scan_push(context))


async def _scheduled_all_scan_push(context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    target_date = get_tw_today()
    try:
        from chip_strategies import is_possible_trading_day

        if not is_possible_trading_day(target_date):
            print(format_cmd_message(f"20:30 全部選股略過非交易日 {target_date.isoformat()}", "定時任務"), flush=True)
            return
    except Exception as exc:
        print(format_cmd_message(f"20:30 全部選股交易日判斷失敗，保守略過：{exc}", "定時任務"), flush=True)
        return

    async def send_text(text: str) -> None:
        await safe_send_bot_message(context.bot, config["chat_id"], text)

    print(format_cmd_message(f"20:30 全部選股開始，資料日期 {target_date.isoformat()}", "定時任務"), flush=True)
    await send_text(f"20:30 交易日全部選股開始\n資料日期：{target_date.isoformat()}")
    await run_selected_scan_reports_core("7", target_date, send_text)
    await send_text(f"20:30 交易日全部選股完成\n資料日期：{target_date.isoformat()}")
    print(format_cmd_message(f"20:30 全部選股完成，資料日期 {target_date.isoformat()}", "定時任務"), flush=True)


async def _scheduled_radar_push(context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    target_date = get_tw_today()
    try:
        from chip_strategies import is_possible_trading_day

        if not is_possible_trading_day(target_date):
            print(f"[{now_timestamp()}] Radar 21:30 略過非交易日 {target_date.isoformat()}", flush=True)
            return
    except Exception as exc:
        print(f"[{now_timestamp()}] Radar 交易日判斷失敗，保守略過：{exc}", flush=True)
        return

    heartbeat = ProgressHeartbeat(
        "Radar 21:30",
        sink=lambda message: print(f"[{now_timestamp()}] {message}", flush=True),
    ).start()

    def progress(message: str) -> None:
        heartbeat.update(message)
        print(f"[{now_timestamp()}] {message}", flush=True)

    try:
        result = await asyncio.to_thread(
            run_radar,
            RadarRequest(
                source="technical",
                report_date=target_date,
                ai_top=5,
                model="minimax",
                ai_comment_enabled=True,
            ),
            scan_settings=config.get("scan_settings", {}),
            config=config,
            progress=progress,
        )
        await context.bot.send_message(chat_id=config["chat_id"], text=format_radar_report(result))
        print(f"[{now_timestamp()}] Radar 21:30 推送完成", flush=True)
    except Exception as exc:
        print(f"[{now_timestamp()}] ❌ Radar 21:30 推送失敗：{exc}", flush=True)
    finally:
        heartbeat.stop()


async def scheduled_news_refresh(context: ContextTypes.DEFAULT_TYPE):
    label = "新聞自動整理"
    if context.job and getattr(context.job, "name", None):
        label = str(context.job.name)
    await enqueue_scheduled_task(context, label, lambda: _scheduled_news_refresh(context))


async def _scheduled_news_refresh(context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    print(f"[{now_timestamp()}] 📰 執行定時新聞整理...")
    try:
        from research_center.orchestrator import ResearchCenter
        center = ResearchCenter()
        db_path = (
            center.config.database_path
            if hasattr(center, "config") and hasattr(center.config, "database_path")
            else Path(__file__).parent / "database" / "stock_research.db"
        )
        repository = NewsRepository(db_path)

        def progress(msg: str) -> None:
            print(msg)

        items, meta = await asyncio.to_thread(run_news_refresh, center, repository, progress, ai_model="minimax")
        digests = run_news_latest(repository)
        text = format_news_digest(digests, period_label="今日")
        await context.bot.send_message(
            chat_id=config["chat_id"],
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        print(f"[{now_timestamp()}] ✅ 新聞整理完成：新增 {meta.get('saved', 0)} 則")
    except Exception as e:
        print(f"[{now_timestamp()}] ⚠️ 新聞整理失敗：{e}")


async def scheduled_portfolio_report(context: ContextTypes.DEFAULT_TYPE):
    await enqueue_scheduled_task(context, "17:45 持股報告", lambda: _scheduled_portfolio_report(context))


async def _scheduled_portfolio_report(context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    attempt = int((context.job.data or {}).get("attempt", 0)) if context.job else 0
    report = await asyncio.to_thread(build_portfolio_report)

    if report.get("status") == "empty":
        print(f"[{now_timestamp()}] ℹ️ portfolio.json 為空，本次不發送庫存籌碼推播")
        return

    if report.get("status") == "retry":
        if context.job_queue and attempt < PORTFOLIO_PUSH_MAX_RETRIES:
            context.job_queue.run_once(
                scheduled_portfolio_report,
                when=PORTFOLIO_PUSH_RETRY_DELAY_SECONDS,
                data={"attempt": attempt + 1},
            )
            print(f"[{now_timestamp()}] ⚠️ 庫存籌碼資料尚未更新，5 分鐘後第 {attempt + 1} 次重試")
            return

        await context.bot.send_message(
            chat_id=config['chat_id'],
            text="今日無法人籌碼資料更新 (可能為非交易日或交易所延遲)",
        )
        return

    message = escape_markdown_v2(str(report.get("message", "")))
    await context.bot.send_message(
        chat_id=config['chat_id'],
        text=message,
        parse_mode='MarkdownV2',
    )


# 新增午報排程：13:50 自動推播台股現貨與台指期日盤，若非交易日或資料未更新則直接略過。
async def scheduled_noon_market_report(context: ContextTypes.DEFAULT_TYPE):
    await enqueue_scheduled_task(context, "13:50 午報", lambda: _scheduled_noon_market_report(context))


async def _scheduled_noon_market_report(context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    attempt = 0
    if context.job and isinstance(context.job.data, dict):
        try:
            attempt = int(context.job.data.get("attempt", 0))
        except (TypeError, ValueError):
            attempt = 0

    try:
        report = await asyncio.to_thread(build_noon_market_report)
    except MarketSummaryError as exc:
        if context.job_queue and attempt < NOON_REPORT_MAX_RETRIES:
            context.job_queue.run_once(
                scheduled_noon_market_report,
                when=NOON_REPORT_RETRY_DELAY_SECONDS,
                data={"attempt": attempt + 1},
            )
            print(
                f"[{now_timestamp()}] ℹ️ 午報略過: {exc} "
                f"retry_in={NOON_REPORT_RETRY_DELAY_SECONDS // 60}m attempt={attempt + 1}"
            )
            return
        print(f"[{now_timestamp()}] ℹ️ 午報略過: {exc} retry_exhausted")
        return
    except Exception as exc:
        print(f"[{now_timestamp()}] ❌ 午報排程失敗: {exc}")
        return

    await context.bot.send_message(chat_id=config['chat_id'], text=report)


async def scheduled_chip_cache_backfill(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data if context.job and isinstance(context.job.data, dict) else {}
    label = str(job_data.get("label") or "籌碼快取回補")
    existing_task = _SCHEDULED_CHIP_BACKFILL_TASKS.get(label)
    if existing_task and not existing_task.done():
        print(format_cmd_message(f"{label} 仍在背景執行，本次略過", "回補背景任務"), flush=True)
        return
    task = context.application.create_task(_scheduled_chip_cache_backfill(job_data))
    task.set_name(label)
    _SCHEDULED_CHIP_BACKFILL_TASKS[label] = task
    task.add_done_callback(lambda done_task, task_label=label: _SCHEDULED_CHIP_BACKFILL_TASKS.pop(task_label, None))
    print(format_cmd_message(f"{label} 已啟動背景執行", "回補背景任務"), flush=True)


async def _scheduled_chip_cache_backfill(job_data: dict | None = None):
    job_data = job_data or {}
    full_backfill = bool(job_data.get("full_backfill", False))
    label = str(job_data.get("label") or ("籌碼快取完整回補" if full_backfill else "籌碼快取今日回補"))
    report_date = get_tw_today()

    print(format_cmd_message(f"0.00% 準備回補資料 {report_date.isoformat()}", label), flush=True)
    try:
        await asyncio.to_thread(
            warmup_chip_data_cache,
            report_date,
            full_backfill,
            False,
            label,
        )
    except Exception as exc:
        print(f"[{now_timestamp()}] ⚠️ 籌碼快取回補失敗：{exc}", flush=True)


async def scheduled_full_backfill_check(context: ContextTypes.DEFAULT_TYPE):
    """Check and run scheduled backfill using policy driver every 2 hours."""
    global _SCHEDULED_BACKFILL_RUNNING, _SCHEDULED_BACKFILL_TASK
    if _SCHEDULED_BACKFILL_TASK and not _SCHEDULED_BACKFILL_TASK.done():
        print(format_cmd_message("上一個背景回補仍在執行，本次略過", "定時回補檢查"), flush=True)
        return

    _SCHEDULED_BACKFILL_STOP_EVENT.clear()
    _SCHEDULED_BACKFILL_RUNNING = True
    _SCHEDULED_BACKFILL_TASK = context.application.create_task(_scheduled_backfill_background())
    _SCHEDULED_BACKFILL_TASK.set_name("定時完整資料回補")
    print(format_cmd_message("已啟動背景定時回補", "定時回補檢查"), flush=True)
    return

    from backfill_service import run_backfill_if_needed

    def progress(message: str) -> None:
        print(format_cmd_message(message, "定時回補檢查"), flush=True)

    # Clear any prior stop signal before starting
    _SCHEDULED_BACKFILL_STOP_EVENT.clear()

    # Mark as running
    _SCHEDULED_BACKFILL_RUNNING = True
    try:
        decision = await asyncio.to_thread(
            run_backfill_if_needed,
            None,
            False,
            progress,
            _SCHEDULED_BACKFILL_STOP_EVENT,
        )

        if decision.status == "skipped":
            reason_text = {
                "already_running": "已有回補執行中",
                "cache_complete": f"已有快取，略過",
                "market_data_unavailable": f"資料尚未發布，略過",
                "today_before_1500": "15:00 前，略過",
                "today_data_check_failed": "今日資料檢查失敗，略過",
                "future_date": "未來日期，略過",
            }.get(decision.reason or "", f"原因：{decision.reason}")
            print(f"[{now_timestamp()}] [定時回補檢查] 跳過 - {reason_text}", flush=True)
            return

        if decision.status == "stopped":
            warning_lines = []
            if decision.result and decision.result.warnings:
                for w in decision.result.warnings[:5]:
                    warning_lines.append(f"  - {w}")
                if len(decision.result.warnings) > 5:
                    warning_lines.append(f"  ...另有 {len(decision.result.warnings) - 5} 筆")
            warning_text = "\n".join(warning_lines)
            print(
                f"[{now_timestamp()}] [定時回補檢查] 已停止：未寫入完成標記"
                + (f"\n警告：\n{warning_text}" if warning_text else ""),
                flush=True,
            )
            return

        if decision.status == "completed" and decision.result:
            result = decision.result
            print(
                f"[{now_timestamp()}] [定時回補檢查] 完成：\n"
                f"  全市場輕量回補：股票宇宙 {result.universe_count} 檔，"
                f"月營收 {result.screening_revenue_count} 檔，"
                f"價量 {result.screening_price_metric_count} 檔，"
                f"技術日線 {result.screening_technical_count} 檔\n"
                f"  候選股中量回補：候選池 {result.candidate_count} 檔，"
                f"毛利率 {result.gross_margin_count} 檔，"
                f"籌碼候選 {result.chip_candidate_count} 檔，"
                f"精選選股 {result.curated_scan_count} 檔\n"
                f"  核心股完整投研回補：核心股 {result.core_research_count} 檔，"
                f"完整投研成功 {result.research_structured_count} 檔，"
                f"快取命中 {len(result.used_cache)} 檔，"
                f"逾時 {result.research_structured_timeout_count} 檔",
                flush=True,
            )
            try:
                health = format_backfill_health_summary(result)
                print(f"[{now_timestamp()}] [定時回補檢查] \n{health}", flush=True)
            except Exception:
                pass
    finally:
        _SCHEDULED_BACKFILL_RUNNING = False


# --- 7. 啟動後初始掃描 ---
async def run_post_init_scan(context: ContextTypes.DEFAULT_TYPE):
    application = context.application
    config = load_config()
    print(f"[{now_timestamp()}] 🔍 啟動後執行監控掃描...")
    init_msg = await asyncio.to_thread(
        build_monitor_scan_report,
        config,
        "🤖 機器人啟動完成\n\n盤中監控初始掃描",
        "目前無突破訊號。",
    )
    try:
        await application.bot.send_message(chat_id=config['chat_id'], text=init_msg)
        print(f"[{now_timestamp()}] ✅ 啟動後監控掃描通知已發送")
    except Exception as e:
        print(f"[{now_timestamp()}] ⚠️ 啟動後監控掃描通知發送失敗：{e}")


async def run_startup_morning_report_if_needed(context: ContextTypes.DEFAULT_TYPE):
    if not is_morning_push_window():
        return

    config = load_config()
    try:
        report = await asyncio.to_thread(build_morning_market_report)
    except MarketSummaryError as exc:
        print(f"[{now_timestamp()}] ℹ️ 啟動晨報略過: {exc}")
        return
    except Exception as exc:
        print(f"[{now_timestamp()}] ❌ 啟動晨報失敗: {exc}")
        return

    try:
        await context.application.bot.send_message(chat_id=config['chat_id'], text=report)
        print(f"[{now_timestamp()}] ✅ 啟動晨報發送成功")
    except Exception as exc:
        print(f"[{now_timestamp()}] ⚠️ 啟動晨報發送失敗: {exc}")


def build_bot_commands() -> list[BotCommand]:
    """Build Telegram slash command menu entries."""
    return [BotCommand(command=command, description=description) for command, description in BOT_COMMAND_SPECS]


async def register_bot_commands(application) -> None:
    """Register slash commands so Telegram can show and filter the command menu."""
    try:
        await application.bot.set_my_commands(build_bot_commands())
        print(f"[{now_timestamp()}] ✅ Telegram slash 指令選單已註冊：{len(BOT_COMMAND_SPECS)} 個指令")
    except Exception as exc:
        print(f"[{now_timestamp()}] ⚠️ Telegram slash 指令選單註冊失敗：{exc}")


async def post_init(application):
    await register_bot_commands(application)
    if application.job_queue:
        # 新增啟動任務：與原本啟動掃描分離，避免市場摘要失敗時影響既有策略掃描。
        application.job_queue.run_once(run_startup_morning_report_if_needed, when=0)

# --- 8. 主程式入口 ---
def main():
    config = load_config()
    # 強化 Timeout 設定
    req = HTTPXRequest(connect_timeout=60, read_timeout=60, write_timeout=60, pool_timeout=60)
    tw_tz = pytz.timezone('Asia/Taipei')

    app = (
        ApplicationBuilder()
        .token(config['api_token'])
        .request(req)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    # 設定每天 12:30 鬧鐘
    if app.job_queue:
        app.job_queue.run_daily(
            scheduled_daily_scan, 
            time=time(hour=12, minute=30, tzinfo=tw_tz)
        )
        # 新增 13:50 午報排程：台股收盤後推播上市櫃指數與台指期日盤摘要。
        app.job_queue.run_daily(
            scheduled_noon_market_report,
            time=time(hour=13, minute=50, tzinfo=tw_tz),
        )
        # 新增 17:45 庫存籌碼推播排程：與 12:30 策略掃描分離，避免兩種通知混在同一條任務鏈。
        app.job_queue.run_daily(
            scheduled_portfolio_report,
            time=time(hour=17, minute=45, tzinfo=tw_tz),
            data={"attempt": 0},
        )
        # 每日 08:45、18:00 新聞自動整理與推播
        app.job_queue.run_daily(
            scheduled_news_refresh,
            time=time(hour=8, minute=45, tzinfo=tw_tz),
        )
        app.job_queue.run_daily(
            scheduled_news_refresh,
            time=time(hour=18, minute=0, tzinfo=tw_tz),
        )
        # 20:30 交易日執行全部選股；若前一個定時推播任務未完成，會排隊接續執行。
        app.job_queue.run_daily(
            scheduled_all_scan_push,
            time=time(hour=20, minute=30, tzinfo=tw_tz),
        )
        # 21:30 交易日 Radar 推播；同樣走定時任務序列佇列。
        app.job_queue.run_daily(
            scheduled_radar_push,
            time=time(hour=21, minute=30, tzinfo=tw_tz),
        )
        # 籌碼資料改為背景慢速回補，不主動推播選股報告；/scan 執行時優先讀快取。
        app.job_queue.run_daily(
            scheduled_chip_cache_backfill,
            time=time(hour=16, minute=30, tzinfo=tw_tz),
            data={"label": "籌碼快取 16:30 今日回補", "full_backfill": False},
        )
        app.job_queue.run_daily(
            scheduled_chip_cache_backfill,
            time=time(hour=18, minute=30, tzinfo=tw_tz),
            data={"label": "籌碼快取 18:30 今日回補", "full_backfill": False},
        )
        app.job_queue.run_daily(
            scheduled_chip_cache_backfill,
            time=time(hour=21, minute=0, tzinfo=tw_tz),
            data={"label": "籌碼快取 21:00 完整回補", "full_backfill": True},
        )
        # 完整資料定時回補：每 2 小時檢查一次，由 policy 判斷目標日期與是否執行。
        from datetime import timedelta
        app.job_queue.run_repeating(
            scheduled_full_backfill_check,
            interval=timedelta(hours=2),
            first=timedelta(minutes=5),
        )

    # 註冊指令
    research_handlers = build_research_handlers(safe_send_reply, safe_reply_document, run_stoppable_command, make_stoppable_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop_running_command))
    # 新增策略監控指令命名：正式以 *_m 區隔監控名單與個人庫存名單。
    app.add_handler(CommandHandler("list_m", list_monitor_stocks))
    app.add_handler(CommandHandler("add_m", add_monitor_stock))
    app.add_handler(CommandHandler("del_m", del_monitor_stock))
    # 新增個人庫存指令：/in、/out、/my 全數委派給 portfolio_manager.py。
    app.add_handler(CommandHandler("in", add_portfolio_command))
    app.add_handler(CommandHandler("out", remove_portfolio_command))
    app.add_handler(CommandHandler("my", list_portfolio_command))
    app.add_handler(CommandHandler("check", make_stoppable_handler("監控掃描", run_scan)))
    app.add_handler(CommandHandler("scan", run_tw_stock_scan))
    app.add_handler(CommandHandler("radar", make_stoppable_handler("Radar 選股雷達", run_radar_command)))
    app.add_handler(CommandHandler("radar_more", run_radar_more_command))
    app.add_handler(CallbackQueryHandler(handle_radar_date_callback, pattern=f"^{RADAR_DATE_CALLBACK_PREFIX}"))
    app.add_handler(CallbackQueryHandler(handle_radar_model_callback, pattern=f"^{RADAR_MODEL_CALLBACK_PREFIX}"))
    app.add_handler(CallbackQueryHandler(handle_scan_strategy_callback, pattern=f"^{SCAN_CALLBACK_PREFIX}"))
    app.add_handler(CallbackQueryHandler(handle_scan_date_callback, pattern=f"^{SCAN_DATE_CALLBACK_PREFIX}"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & (filters.Regex(r"https?://") | filters.Entity("url")), research_handlers["news_url_message"]))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, research_handlers["ai_menu_text"]))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_scan_date_text_input), group=1)
    app.add_handler(CommandHandler("backfill", make_stoppable_handler("完整資料回補", manual_full_backfill)))
    app.add_handler(CommandHandler("export", make_stoppable_handler("匯出股票資料", export_stock)))
    # 新增市場摘要指令：/morning 查晨報，/noon 與 /tw_market 共用午報處理器。
    app.add_handler(CommandHandler("morning", make_stoppable_handler("晨報", morning_market_summary_command)))
    app.add_handler(CommandHandler("noon", make_stoppable_handler("午報", noon_market_summary_command)))
    app.add_handler(CommandHandler("tw_market", make_stoppable_handler("台股午報", noon_market_summary_command)))
    # 新增指令註冊：支援 /stock_chart 生成台股個股的互動式 HTML 技術分析圖表。
    app.add_handler(CommandHandler("stock_chart", make_stoppable_handler("個股圖表匯出", export_stock_chart)))
    # 新增指令註冊：支援 /tmf_chart 生成 TMF 的互動式 Lightweight Charts HTML 報表。
    app.add_handler(CommandHandler("tmf_chart", make_stoppable_handler("TMF 圖表匯出", export_tmf_chart)))
    app.add_handler(CommandHandler("research", research_handlers["research"]))
    app.add_handler(CommandHandler("macro", research_handlers["macro"]))
    app.add_handler(CommandHandler("theme", research_handlers["theme"]))
    app.add_handler(CommandHandler("theme_radar", research_handlers["theme_radar"]))
    app.add_handler(CommandHandler("theme_flow", research_handlers["theme_flow"]))
    app.add_handler(CommandHandler("sector_strength", research_handlers["sector_strength"]))
    app.add_handler(CommandHandler("value_scan", research_handlers["value_scan"]))
    app.add_handler(CommandHandler("news", research_handlers["news"]))
    app.add_handler(CommandHandler("news_detail", research_handlers["news_detail"]))
    app.add_handler(CommandHandler("news_save", research_handlers["news_save"]))
    app.add_handler(CommandHandler("data_status", research_handlers["data_status"]))
    app.add_handler(CommandHandler("backfill_status", research_handlers["backfill_status"]))
    app.add_handler(CommandHandler("news_status", research_handlers["news_status"]))
    app.add_handler(CommandHandler("report", research_handlers["report"]))
    app.add_handler(CommandHandler("help", research_handlers["help"]))
    app.add_handler(CommandHandler("ai_help", research_handlers["ai_help"]))
    app.add_handler(CommandHandler("topic_maintain", research_handlers["topic_maintain"]))
    app.add_handler(CommandHandler("topic_review", research_handlers["topic_review"]))
    app.add_handler(CommandHandler("topic_confirm", research_handlers["topic_confirm"]))
    app.add_handler(CommandHandler("topic_reject", research_handlers["topic_reject"]))
    app.add_handler(CommandHandler("topic_profiles", research_handlers["topic_profiles"]))
    app.add_handler(CommandHandler("topic_reset", research_handlers["topic_reset"]))
    app.add_handler(CommandHandler("topic_seed_prompt", research_handlers["topic_seed_prompt"]))
    app.add_handler(CommandHandler("topic_import", research_handlers["topic_import"]))
    app.add_handler(CommandHandler("topic_source_sync", research_handlers["topic_source_sync"]))
    app.add_handler(CallbackQueryHandler(research_handlers["ai_menu_callback"], pattern="^ai_menu:"))
    app.add_handler(MessageHandler(filters.Document.ALL, research_handlers["ai_menu_document"]))

    print(f"[{now_timestamp()}] 🚀 策略機器人啟動中，定時設定：12:30 監控掃描、13:50 午報、17:45 庫存推播、16:30/18:30/21:00 籌碼快取回補...")
    app.run_polling(bootstrap_retries=-1)

if __name__ == "__main__":
    main()


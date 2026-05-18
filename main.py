import json
import asyncio
import telegram
import pytz
from datetime import date, datetime, time, timedelta
import threading
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest
import curated_scan_service
from research_center.recent_scans import save_recent_scan_result

from chip_strategies import (
    CHIP_STRATEGY_NAMES,
    STRATEGY_DEFINITIONS,
    build_chip_reports,
    get_tw_today,
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
from tmf_chart_service import TmfChartError, build_tmf_chart_report, parse_tmf_chart_args

SCAN_CALLBACK_PREFIX = "scan_strategy:"
NOON_REPORT_MAX_RETRIES = 6
NOON_REPORT_RETRY_DELAY_SECONDS = 30 * 60
ACTIVE_USER_TASKS: dict[int, asyncio.Task] = {}
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
    "7": ["financial", "chip_1", "chip_2", "chip_3", "chip_4", "technical"],
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
                print(f"⚠️ 排程訊息第 {i+1} 次發送失敗: {e}")
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
        sent = False
        for i in range(3):
            try:
                await message.reply_text(chunk, reply_markup=reply_markup if not sent else None)
                sent = True
                reply_markup = None
                break
            except Exception as e:
                print(f"⚠️ 第 {i+1} 次發送失敗: {e}")
                await asyncio.sleep(3)
        if not sent:
            print("❌ 訊息發送失敗，已放棄本段回覆")
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
            print(f"⚠️ 檔案上傳第 {attempt + 1} 次逾時/網路失敗: {exc}")
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
        print(f"⏹️ 使用者已停止任務：{label}")
    finally:
        if ACTIVE_USER_TASKS.get(chat_id) is current_task:
            ACTIVE_USER_TASKS.pop(chat_id, None)


# Per-user stop events for manual backfill
_USER_BACKFILL_STOP_EVENTS: dict[int, threading.Event] = {}

# Global stop event for scheduled backfill (set by /stop)
_SCHEDULED_BACKFILL_STOP_EVENT = threading.Event()

# Flag indicating scheduled backfill is currently running
_SCHEDULED_BACKFILL_RUNNING = False


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
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send_reply(update, "🤖 策略機器人已就緒！\n/list_m - 查看策略監控清單\n/add_m 代碼 名稱 - 加入策略監控\n/del_m 代碼 - 刪除策略監控\n/in 代碼或名稱 - 加入個人庫存\n/out 代碼或名稱 - 移除個人庫存\n/my - 查看個人庫存\n/check - 監控清單掃描\n/scan - 全市場選股掃描\n/backfill [YYYY-MM-DD] [force] - 建立選股+投研候選池並完整回補本地資料\n/export 代碼 - 匯出資料\n/morning - 晨間美股與台指期夜盤\n/noon - 台股收盤與台指期日盤\n/tw_market - 台股收盤與台指期日盤\n/stock_chart 代碼 起日 迄日 頻率 - 匯出個股互動圖表\n/tmf_chart 起日 迄日 盤別 頻率 - 匯出 TMF 互動圖表\n/research 代號 - AI 個股研究\n/macro [市場] [主題] - AI 宏觀研究\n/theme 題材 --top 20 - AI 題材研究\n/value_scan [候選池] --top 30 - AI 價值重估掃描\n/report latest - 查詢最近 AI 報告\n/ai_help - AI 投研指令說明\n/stop - 停止目前執行中的耗時任務")

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
        print(f"❌ /morning 執行失敗: {exc}")
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
        print(f"❌ /noon 執行失敗: {exc}")
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


def build_scan_strategy_keyboard(report_date: date | None = None) -> InlineKeyboardMarkup:
    # 新增 /scan 的互動式按鈕選單，避免使用者每次輸入指令就直接執行所有高成本掃描。
    target_date = report_date or get_tw_today()
    suffix = f":{target_date.isoformat()}"
    rows = [
        [InlineKeyboardButton("1. 財報營收選股", callback_data=f"{SCAN_CALLBACK_PREFIX}1{suffix}")],
        [InlineKeyboardButton("2. 60 日法人動態選股", callback_data=f"{SCAN_CALLBACK_PREFIX}2{suffix}")],
        [InlineKeyboardButton("3. 投信認養股", callback_data=f"{SCAN_CALLBACK_PREFIX}3{suffix}")],
        [InlineKeyboardButton("4. 法人持股比例增加", callback_data=f"{SCAN_CALLBACK_PREFIX}4{suffix}")],
        [InlineKeyboardButton("5. 每週大戶持股選股", callback_data=f"{SCAN_CALLBACK_PREFIX}5{suffix}")],
        [InlineKeyboardButton("6. 技術面選股", callback_data=f"{SCAN_CALLBACK_PREFIX}6{suffix}")],
        [InlineKeyboardButton("7. 全部執行", callback_data=f"{SCAN_CALLBACK_PREFIX}7{suffix}")],
        [InlineKeyboardButton("8. 精選選股", callback_data=f"{SCAN_CALLBACK_PREFIX}8{suffix}")],
    ]
    return InlineKeyboardMarkup(rows)


async def run_selected_scan_reports(update: Update, selection: str, report_date: date | None = None):
    selected_keys = SCAN_SELECTIONS.get(selection)
    if not selected_keys:
        await safe_send_reply(update, "❌ 無效的選股策略選項。")
        return

    target_date = report_date or get_tw_today()
    menu_label = SCAN_MENU_LABELS.get(selection, selection)
    print(f"[選股進度][{menu_label}] 0.00% 收到 /scan 選股任務，目標日期 {target_date.isoformat()}", flush=True)

    if "curated" in selected_keys:
        print(f"[選股進度][{menu_label}] 10.00% 開始精選交叉比對", flush=True)
        await safe_send_reply(update, "正在比對技術面、營收財報與法人大戶策略，整理重複命中的精選名單...")
        try:
            config = load_config()
            curated_result = await asyncio.to_thread(
                curated_scan_service.build_curated_scan_result,
                config.get("scan_settings", {}),
                target_date,
            )
            print(f"[選股進度][{menu_label}] 90.00% 精選報告產生完成，準備傳送 Telegram", flush=True)
            save_recent_scan_result(menu_label, target_date, curated_result.report_text, curated_result.selected_codes)
            await safe_send_reply(update, curated_result.report_text)
            print(f"[選股進度][{menu_label}] 100.00% 完成", flush=True)
        except Exception as exc:
            print(f"❌ /scan 精選選股失敗: {exc}")
            await safe_send_reply(update, "⚠️ 精選選股產生失敗，請稍後再試。")
        return

    # 完成一段就先送一段，避免全部選股遇到單一資料源變慢時使用者長時間沒有任何回應。
    if "financial" in selected_keys:
        try:
            print(f"[選股進度][{menu_label}] 10.00% 開始財報營收選股", flush=True)
            config = load_config()
            financial_report = await asyncio.to_thread(
                run_tw_market_scan,
                False,
                None,
                config.get("scan_settings", {}),
            )
            print(f"[選股進度][{menu_label}] 35.00% 財報營收報告完成，準備傳送 Telegram", flush=True)
            await safe_send_reply(update, financial_report)
        except Exception as exc:
            print(f"❌ /scan 財報選股失敗: {exc}")
            await safe_send_reply(update, "⚠️ 財報選股產生失敗，會繼續嘗試其他已選策略。")

    chip_keys = [key for key in selected_keys if key.startswith("chip_")]
    has_technical = "technical" in selected_keys
    if not chip_keys and not has_technical:
        print(f"[選股進度][{menu_label}] 100.00% 完成", flush=True)
        return

    if chip_keys:
        chip_progress_end = 70.0 if has_technical else 90.0
        print(f"[選股進度][{menu_label}] 40.00% 開始籌碼策略資料整理", flush=True)
        await safe_send_reply(update, "籌碼選股資料整理中。")
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
            print(f"❌ /scan 籌碼選股失敗: {exc}")
            await safe_send_reply(update, "⚠️ 籌碼選股產生失敗，請稍後再試或先單獨執行其他策略。")
            return

        for index, key in enumerate(chip_keys, start=1):
            progress = chip_progress_end + index / max(1, len(chip_keys)) * 5.0
            print(f"[選股進度][{menu_label}] {progress:.2f}% 傳送 {CHIP_STRATEGY_NAMES.get(key, key)} 報告", flush=True)
            await safe_send_reply(update, chip_reports[key])

    # NEW: 技術面選股路由
    if has_technical:
        technical_start_progress = 75.0 if chip_keys or "financial" in selected_keys else 10.0
        print(f"[選股進度][{menu_label}] {technical_start_progress:.2f}% 開始技術面選股", flush=True)
        await safe_send_reply(update, "技術面選股資料整理中。")
        try:
            config = load_config()
            technical_report = await asyncio.to_thread(
                ts.build_technical_scan_report,
                config.get("scan_settings", {}),
                target_date,
            )
            print(f"[選股進度][{menu_label}] 98.00% 技術面報告完成，準備傳送 Telegram", flush=True)
            await safe_send_reply(update, technical_report)
        except Exception as exc:
            print(f"❌ /scan 技術面選股失敗: {exc}")
            await safe_send_reply(update, "⚠️ 技術面選股產生失敗，請稍後再試。")
    print(f"[選股進度][{menu_label}] 100.00% 完成", flush=True)


async def manual_full_backfill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /backfill command: build candidate pool and warm up local data caches."""
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
        print(f"[完整回補] {message}", flush=True)

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
        print(f"❌ [完整回補] 失敗：{exc}", flush=True)
        await safe_send_reply(update, f"❌ 完整資料回補失敗：{exc}")
    finally:
        _USER_BACKFILL_STOP_EVENTS.pop(chat_id, None)


async def run_tw_stock_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 新增 /scan 互動流程：先讓使用者選策略，再由 callback handler 執行對應掃描。
    try:
        report_date = parse_scan_report_date(context.args)
    except ValueError as exc:
        await safe_send_reply(update, f"❌ {exc}\n範例：/scan 2026-05-05")
        return

    text = f"{SCAN_MENU_TEXT}\n\n目標資料日期：{report_date.isoformat()}"
    await safe_send_reply(update, text, reply_markup=build_scan_strategy_keyboard(report_date))


async def handle_scan_strategy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    payload = query.data.replace(SCAN_CALLBACK_PREFIX, "", 1)
    payload_parts = payload.split(":", 1)
    selection = payload_parts[0]
    try:
        report_date = datetime.strptime(payload_parts[1], "%Y-%m-%d").date() if len(payload_parts) > 1 else get_tw_today()
    except ValueError:
        report_date = get_tw_today()
    selected_keys = SCAN_SELECTIONS.get(selection)
    if not selected_keys:
        await query.edit_message_text("❌ 無效的選股策略選項。")
        return

    menu_label = SCAN_MENU_LABELS[selection]

    # 新增 callback 狀態提示：先更新選單訊息，讓使用者知道系統已接受操作並開始計算。
    await query.edit_message_text(f"已選擇：{menu_label}\n目標資料日期：{report_date.isoformat()}\n開始執行，請稍候...")
    await run_stoppable_command(
        update,
        f"選股：{menu_label}",
        lambda: run_selected_scan_reports(update, selection, report_date),
    )

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
        print(f"❌ /export 執行失敗 {raw_symbol}: {exc}")
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
        print(f"❌ /stock_chart 執行失敗: {exc}")
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
        print(f"❌ /tmf_chart 執行失敗: {exc}")
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
    config = load_config()
    print("🔍 執行 12:30 監控掃描...")
    msg = await asyncio.to_thread(
        build_monitor_scan_report,
        config,
        "📌 12:30 監控突破通知",
        "目前沒有符合條件的突破訊號。",
    )
    try:
        await context.bot.send_message(chat_id=config['chat_id'], text=msg)
        print("✅ 12:30 監控通知已發送")
    except Exception as e:
        print(f"⚠️ 12:30 監控通知發送失敗：{e}")


async def scheduled_portfolio_report(context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    attempt = int((context.job.data or {}).get("attempt", 0)) if context.job else 0
    report = await asyncio.to_thread(build_portfolio_report)

    if report.get("status") == "empty":
        print("ℹ️ portfolio.json 為空，本次不發送庫存籌碼推播")
        return

    if report.get("status") == "retry":
        if context.job_queue and attempt < PORTFOLIO_PUSH_MAX_RETRIES:
            context.job_queue.run_once(
                scheduled_portfolio_report,
                when=PORTFOLIO_PUSH_RETRY_DELAY_SECONDS,
                data={"attempt": attempt + 1},
            )
            print(f"⚠️ 庫存籌碼資料尚未更新，5 分鐘後第 {attempt + 1} 次重試")
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
                f"ℹ️ 午報略過: {exc} "
                f"retry_in={NOON_REPORT_RETRY_DELAY_SECONDS // 60}m attempt={attempt + 1}"
            )
            return
        print(f"ℹ️ 午報略過: {exc} retry_exhausted")
        return
    except Exception as exc:
        print(f"❌ 午報排程失敗: {exc}")
        return

    await context.bot.send_message(chat_id=config['chat_id'], text=report)


async def scheduled_chip_cache_backfill(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data if context.job and isinstance(context.job.data, dict) else {}
    full_backfill = bool(job_data.get("full_backfill", False))
    label = str(job_data.get("label") or ("籌碼快取完整回補" if full_backfill else "籌碼快取今日回補"))
    report_date = get_tw_today()

    print(f"[開始][{label}] 0.00% 準備回補資料 {report_date.isoformat()}", flush=True)
    try:
        await asyncio.to_thread(
            warmup_chip_data_cache,
            report_date,
            full_backfill,
            False,
            label,
        )
    except Exception as exc:
        print(f"⚠️ 籌碼快取回補失敗：{exc}", flush=True)


async def scheduled_full_backfill_check(context: ContextTypes.DEFAULT_TYPE):
    """Check and run scheduled backfill using policy driver every 2 hours."""
    global _SCHEDULED_BACKFILL_RUNNING
    from backfill_service import run_backfill_if_needed

    def progress(message: str) -> None:
        print(f"[定時回補檢查] {message}", flush=True)

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
            print(f"[定時回補檢查] 跳過 - {reason_text}", flush=True)
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
                f"[定時回補檢查] 已停止：未寫入完成標記"
                + (f"\n警告：\n{warning_text}" if warning_text else ""),
                flush=True,
            )
            return

        if decision.status == "completed" and decision.result:
            result = decision.result
            print(
                f"[定時回補檢查] 完成：\n"
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
                print(f"[定時回補檢查] \n{health}", flush=True)
            except Exception:
                pass
    finally:
        _SCHEDULED_BACKFILL_RUNNING = False


# --- 7. 啟動後初始掃描 ---
async def run_post_init_scan(context: ContextTypes.DEFAULT_TYPE):
    application = context.application
    config = load_config()
    print("🔍 啟動後執行監控掃描...")
    init_msg = await asyncio.to_thread(
        build_monitor_scan_report,
        config,
        "🤖 機器人啟動完成\n\n盤中監控初始掃描",
        "目前無突破訊號。",
    )
    try:
        await application.bot.send_message(chat_id=config['chat_id'], text=init_msg)
        print("✅ 啟動後監控掃描通知已發送")
    except Exception as e:
        print(f"⚠️ 啟動後監控掃描通知發送失敗：{e}")


async def run_startup_morning_report_if_needed(context: ContextTypes.DEFAULT_TYPE):
    if not is_morning_push_window():
        return

    config = load_config()
    try:
        report = await asyncio.to_thread(build_morning_market_report)
    except MarketSummaryError as exc:
        print(f"ℹ️ 啟動晨報略過: {exc}")
        return
    except Exception as exc:
        print(f"❌ 啟動晨報失敗: {exc}")
        return

    try:
        await context.application.bot.send_message(chat_id=config['chat_id'], text=report)
        print("✅ 啟動晨報發送成功")
    except Exception as exc:
        print(f"⚠️ 啟動晨報發送失敗: {exc}")


async def post_init(application):
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
    app.add_handler(CallbackQueryHandler(handle_scan_strategy_callback, pattern=f"^{SCAN_CALLBACK_PREFIX}"))
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
    app.add_handler(CommandHandler("value_scan", research_handlers["value_scan"]))
    app.add_handler(CommandHandler("report", research_handlers["report"]))
    app.add_handler(CommandHandler("ai_help", research_handlers["ai_help"]))
    app.add_handler(CallbackQueryHandler(research_handlers["ai_menu_callback"], pattern="^ai_menu:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, research_handlers["ai_menu_text"]))

    print("🚀 策略機器人啟動中，定時設定：12:30 監控掃描、13:50 午報、17:45 庫存推播、16:30/18:30/21:00 籌碼快取回補...")
    app.run_polling(bootstrap_retries=-1)

if __name__ == "__main__":
    main()




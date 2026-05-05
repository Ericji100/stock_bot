import yfinance as yf
import pandas as pd
import json
import asyncio
import telegram
import httpx
import pytz
from datetime import time, datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

from chip_strategies import (
    STRATEGY_DEFINITIONS,
    build_chip_reports,
    get_tw_today,
    latest_weekly_snapshot_date,
    mark_weekly_report_sent,
    should_run_startup_weekly_fallback,
)
from data_fetcher import StockExportError, StockNotFoundError
from export_service import build_stock_export_workbook
from market_summary import (
    MarketSummaryError,
    build_morning_market_report,
    build_noon_market_report,
    is_morning_push_window,
)
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
from stock_scanner import run_scan as run_tw_market_scan
from tmf_chart_service import TmfChartError, build_tmf_chart_report, parse_tmf_chart_args

OFFICIAL_NAME_CACHE = {}
OFFICIAL_SYMBOL_CACHE = {}
OFFICIAL_NAME_CACHE_EXPIRES_AT = None
OFFICIAL_NAME_CACHE_TTL = timedelta(hours=12)
TWSE_NAME_API_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_NAME_API_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
SCAN_CALLBACK_PREFIX = "scan_strategy:"
SCAN_MENU_TEXT = (
    "請選擇選股策略：\n"
    "1. 財報營收選股\n"
    "2. 60 日法人動態選股\n"
    "3. 投信認養股\n"
    "4. 法人持股比例增加\n"
    "5. 每週大戶持股選股\n"
    "6. 全部執行"
)
SCAN_SELECTIONS = {
    "1": ["financial"],
    "2": ["chip_1"],
    "3": ["chip_2"],
    "4": ["chip_3"],
    "5": ["chip_4"],
    "6": ["financial", "chip_1", "chip_2", "chip_3", "chip_4"],
}
SCAN_MENU_LABELS = {
    "1": STRATEGY_DEFINITIONS["financial"]["menu"],
    "2": STRATEGY_DEFINITIONS["chip_1"]["menu"],
    "3": STRATEGY_DEFINITIONS["chip_2"]["menu"],
    "4": STRATEGY_DEFINITIONS["chip_3"]["menu"],
    "5": STRATEGY_DEFINITIONS["chip_4"]["menu"],
    "6": STRATEGY_DEFINITIONS["all"]["menu"],
}

# --- 1. 檔案管理 ---
def load_config():
    with open('config.json', 'r', encoding='utf-8') as f:
        return json.load(f)

def save_config(config):
    with open('config.json', 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)


def fetch_official_stock_name_cache():
    global OFFICIAL_NAME_CACHE_EXPIRES_AT

    now = datetime.now()
    if OFFICIAL_NAME_CACHE and OFFICIAL_NAME_CACHE_EXPIRES_AT and now < OFFICIAL_NAME_CACHE_EXPIRES_AT:
        return OFFICIAL_NAME_CACHE

    updated_cache = {}
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True, verify=False) as client:
            twse_response = client.get(TWSE_NAME_API_URL)
            twse_response.raise_for_status()
            for item in twse_response.json():
                code = str(item.get('公司代號', '')).strip()
                name = str(item.get('公司簡稱') or item.get('公司名稱') or '').strip()
                if code and name:
                    updated_cache[f"{code}.TW"] = name
                    updated_cache.setdefault(code, name)

            tpex_response = client.get(TPEX_NAME_API_URL)
            tpex_response.raise_for_status()
            for item in tpex_response.json():
                code = str(item.get('SecuritiesCompanyCode', '')).strip()
                name = str(item.get('CompanyAbbreviation') or item.get('CompanyName') or '').strip()
                if code and name:
                    updated_cache[f"{code}.TWO"] = name
                    updated_cache.setdefault(code, name)

        OFFICIAL_NAME_CACHE.clear()
        OFFICIAL_NAME_CACHE.update(updated_cache)

        OFFICIAL_SYMBOL_CACHE.clear()
        for symbol in updated_cache:
            if '.' in symbol:
                OFFICIAL_SYMBOL_CACHE[symbol.split('.', 1)[0]] = symbol

        OFFICIAL_NAME_CACHE_EXPIRES_AT = now + OFFICIAL_NAME_CACHE_TTL
    except Exception as e:
        print(f"取得官方股名失敗，改用既有名稱設定: {e}")

    return OFFICIAL_NAME_CACHE


def get_official_stock_name(symbol):
    normalized_symbol = str(symbol).upper().strip()
    if not normalized_symbol:
        return ''

    cache = fetch_official_stock_name_cache()
    if normalized_symbol in cache:
        return cache[normalized_symbol]

    base_symbol = normalized_symbol.split('.', 1)[0]
    return cache.get(base_symbol, '')


def get_canonical_stock_symbol(symbol):
    normalized_symbol = str(symbol).upper().strip()
    if not normalized_symbol:
        return ''

    fetch_official_stock_name_cache()
    base_symbol = normalized_symbol.split('.', 1)[0]
    canonical_symbol = OFFICIAL_SYMBOL_CACHE.get(base_symbol)

    if '.' in normalized_symbol:
        return normalized_symbol if normalized_symbol in OFFICIAL_NAME_CACHE else (canonical_symbol or normalized_symbol)

    return canonical_symbol or normalized_symbol


def normalize_stock_entry(stock):
    if isinstance(stock, str):
        symbol = get_canonical_stock_symbol(stock)
        return {'symbol': symbol, 'name': ''}

    if isinstance(stock, dict):
        symbol = get_canonical_stock_symbol(stock.get('symbol', ''))
        name = str(stock.get('name', '')).strip()
        if symbol:
            return {'symbol': symbol, 'name': name}

    return None


def get_monitor_stocks(config):
    normalized_stocks = []
    for stock in config.get('monitor_stocks', []):
        normalized_stock = normalize_stock_entry(stock)
        if normalized_stock:
            if not normalized_stock['name']:
                normalized_stock['name'] = get_official_stock_name(normalized_stock['symbol'])
            normalized_stocks.append(normalized_stock)
    return normalized_stocks


def format_stock_display(stock):
    if stock.get('name'):
        return f"{stock['symbol']} ({stock['name']})"
    return stock['symbol']


def find_stock_index(stocks, symbol):
    symbol = get_canonical_stock_symbol(symbol)
    for index, stock in enumerate(stocks):
        normalized_stock = normalize_stock_entry(stock)
        if normalized_stock and normalized_stock['symbol'] == symbol:
            return index
    return -1


def get_tw_local_now():
    return datetime.now(pytz.timezone('Asia/Taipei'))


def build_official_realtime_channel(symbol):
    normalized_symbol = str(symbol).upper().strip()
    code = normalized_symbol.split('.', 1)[0]
    suffix = normalized_symbol.split('.', 1)[1] if '.' in normalized_symbol else ''
    market = 'otc' if suffix == 'TWO' else 'tse'
    return f"{market}_{code}.tw"


def parse_official_price_value(raw_value):
    if raw_value in (None, '', '-'):
        return None

    first_value = str(raw_value).split('_', 1)[0].strip()
    if first_value in ('', '-'):
        return None

    try:
        return float(first_value)
    except (TypeError, ValueError):
        return None


def pick_official_quote_price(msg):
    trade_price = parse_official_price_value(msg.get('z'))
    if trade_price is not None:
        return trade_price, '官方成交價'

    best_bid = parse_official_price_value(msg.get('b'))
    best_ask = parse_official_price_value(msg.get('a'))
    if best_bid is not None and best_ask is not None:
        return (best_bid + best_ask) / 2.0, '官方委買賣中間價'
    if best_bid is not None:
        return best_bid, '官方委買價'
    if best_ask is not None:
        return best_ask, '官方委賣價'

    return None, None


def get_official_realtime_price(symbol):
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True, verify=False) as client:
            response = client.get(
                'https://mis.twse.com.tw/stock/api/getStockInfo.jsp',
                params={
                    'ex_ch': build_official_realtime_channel(symbol),
                    'json': '1',
                    'delay': '0',
                },
                headers={'Referer': 'https://mis.twse.com.tw/stock/fibest.jsp'},
            )
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        print(f"⚠️ 取得官方即時報價失敗 {symbol}: {e}")
        return None, None

    msg = (data.get('msgArray') or [{}])[0]
    official_price, official_source = pick_official_quote_price(msg)
    quote_date = str(msg.get('d') or '').strip()

    return official_price, quote_date, official_source


def get_current_market_price(symbol, fallback_price=None):
    today_tw = get_tw_local_now().strftime('%Y%m%d')
    yahoo_price = None
    yahoo_quote_date = None

    try:
        ticker = yf.Ticker(symbol)
        try:
            fast_info = ticker.fast_info
            last_price = fast_info.get('lastPrice') if fast_info else None
            if last_price not in (None, 0):
                yahoo_price = float(last_price)
        except Exception:
            pass

        try:
            info = ticker.info
            market_timestamp = info.get('regularMarketTime')
            if market_timestamp:
                yahoo_quote_date = datetime.fromtimestamp(
                    market_timestamp,
                    tz=pytz.utc,
                ).astimezone(pytz.timezone('Asia/Taipei')).strftime('%Y%m%d')

            for key in ('regularMarketPrice', 'currentPrice'):
                value = info.get(key)
                if value not in (None, 0):
                    yahoo_price = float(value)
                    break
        except Exception:
            pass
    except Exception as e:
        print(f"⚠️ 取得盤中現價失敗 {symbol}: {e}")

    if yahoo_price is not None and yahoo_quote_date == today_tw:
        return yahoo_price, 'Yahoo 盤中'

    official_price, official_quote_date, official_source = get_official_realtime_price(symbol)
    if official_price is not None and official_quote_date == today_tw:
        return official_price, official_source

    return fallback_price, '前日收盤 fallback'

# --- 2. 策略邏輯 A：原始 21MA 突破 ---
def check_signal(stock):
    symbol = stock['symbol']
    stock_display = format_stock_display(stock)
    print(f"🔍 正在檢查(突破21MA) {stock_display}...")
    try:
        # 修改點：增加到 100d 並加入 auto_adjust=False
        df = yf.download(symbol, period="500d", interval="1d", progress=False, auto_adjust=False)
        if df.empty or len(df) < 30: return None

        # 處理多層索引
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df['MA21'] = df['Close'].rolling(window=21).mean()

        latest_close = df['Close'].iloc[-1].item()
        current_price, price_source = get_current_market_price(symbol, fallback_price=latest_close)
        today_m = df['MA21'].iloc[-1].item()
        yest_p = df['Close'].iloc[-2].item()
        yest_m = df['MA21'].iloc[-2].item()

        if yest_p < yest_m and current_price > today_m:
            stop_loss = df['Low'].iloc[-3:].min().item()
            return (f"🚀 【21MA 突破】\n"
                    f"股票：{stock_display}\n"
                    f"現價：{current_price:,.2f} (MA21: {today_m:,.2f})\n"
                    f"現價來源：{price_source}\n"
                    f"建議停損線：{stop_loss:,.2f} (前三日低)")
    except Exception as e:
        print(f"❌ 基礎判斷出錯 {stock_display}: {e}")
    return None

# --- 2. 策略邏輯 B：MACD 翻紅後回測突破 (進階) ---
def check_advanced_signal(stock):
    symbol = stock['symbol']
    stock_display = format_stock_display(stock)
    print(f"🔍 正在檢查(回測突破) {stock_display}...")
    try:
        # 1. 抓取資料 (確保 auto_adjust=False 以對齊價格)
        df = yf.download(symbol, period="500d", interval="1d", progress=False, auto_adjust=False)
        if df.empty or len(df) < 150: return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # 2. 計算指標
        df['MA21'] = df['Close'].rolling(window=21).mean()
        ema21 = df['Close'].ewm(span=21, adjust=False).mean()
        ema55 = df['Close'].ewm(span=55, adjust=False).mean()
        df['DIF'] = ema21 - ema55
        df['DEA'] = df['DIF'].ewm(span=55, adjust=False).mean()
        df['MACD_Hist'] = df['DIF'] - df['DEA']

        # 3. 尋找「當前這波」紅柱的起點
        # 如果今天不是紅柱，直接結束
        if df['MACD_Hist'].iloc[-1] <= 0:
            return None

        # 往前找最近一個綠柱(<=0)的日子
        green_days = df[df['MACD_Hist'] <= 0]
        if green_days.empty: return None
        
        red_start_date = green_days.index[-1] + pd.Timedelta(days=1)
        
        # 定義「翻紅以來」到「昨天」的區間 (用來找前高與確認回測)
        df_red_zone_past = df.loc[red_start_date : df.index[-2]]
        
        if df_red_zone_past.empty:
            return None # 剛翻紅的第一天，通常還沒經過回測，不發訊號

        # --- 驗證三大條件 (與回測邏輯一致) ---
        
        # 條件 A: 過去這段紅柱期間，是否曾碰觸過 21MA (Low <= MA21)
        had_touch = (df_red_zone_past['Low'] <= df_red_zone_past['MA21']).any()
        
        # 條件 B: 突破「這段紅柱」以來的所有高點
        period_high = df_red_zone_past['High'].max()
        latest_close = df['Close'].iloc[-1].item()
        current_price, price_source = get_current_market_price(symbol, fallback_price=latest_close)
        
        # 條件 C: 今日必須站在 MA21 之上
        above_ma21 = current_price > df['MA21'].iloc[-1].item()

        # 4. 輸出通知
        if had_touch and current_price > period_high and above_ma21:
            # 額外計算停損線提供給使用者參考
            stop_loss = df['Low'].iloc[-3:].min().item()
            return (f"🔥 【回測突破】\n"
                    f"股票：{stock_display}\n"
                    f"現價：{current_price:.2f}\n"
                    f"現價來源：{price_source}\n"
                    f"原因：已完成 21MA 回測，今日突破波段前高 {period_high:.2f}！\n"
                    f"建議停損線：{stop_loss:.2f} (前三日低)")
            
    except Exception as e:
        print(f"❌ 進階判斷出錯 {stock_display}: {e}")
    return None

# --- 3. 新增策略 C：105MA 突破 (邏輯與策略 A 一致) ---
def check_ma105_signal(stock):
    symbol = stock['symbol']
    stock_display = format_stock_display(stock)
    print(f"🔍 正在檢查(突破105MA) {stock_display}...")
    try:
        # 抓取 500 天資料確保足以計算 105MA
        df = yf.download(symbol, period="500d", interval="1d", progress=False, auto_adjust=False)
        if df.empty or len(df) < 110: return None # 105MA 至少需要 105 筆資料

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # 計算 105MA
        df['MA105'] = df['Close'].rolling(window=105).mean()

        latest_close = df['Close'].iloc[-1].item()
        current_price, price_source = get_current_market_price(symbol, fallback_price=latest_close)
        today_m = df['MA105'].iloc[-1].item()
        yest_p = df['Close'].iloc[-2].item()
        yest_m = df['MA105'].iloc[-2].item()

        # 判斷穿越邏輯：昨日在下，今日在上
        if yest_p < yest_m and current_price > today_m:
            stop_loss = df['Low'].iloc[-3:].min().item()
            return (f"🚀 【105MA 突破】\n"
                    f"股票：{stock_display}\n"
                f"現價：{current_price:,.2f} (MA105: {today_m:,.2f})\n"
                    f"現價來源：{price_source}\n"
                    f"建議停損線：{stop_loss:,.2f} (前三日低)")
    except Exception as e:
        print(f"❌ 105MA 判斷出錯 {stock_display}: {e}")
    return None

# --- 4. 穩定發送機制 ---
async def safe_send_reply(update: Update, text: str, reply_markup=None):
    """具備自動重試的發送函式"""
    message = update.effective_message
    if message is None and update.callback_query is not None:
        message = update.callback_query.message
    if message is None:
        raise ValueError("找不到可回覆的 Telegram message")

    chunks = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= 4000:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, 4000)
        if split_at == -1:
            split_at = 4000
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()

    for chunk in chunks or [text]:
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

# --- 5. 指令處理器 ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send_reply(update, "🤖 策略機器人已就緒！\n/list_m - 查看策略監控清單\n/add_m 代碼 名稱 - 加入策略監控\n/del_m 代碼 - 刪除策略監控\n/in 代碼或名稱 - 加入個人庫存\n/out 代碼或名稱 - 移除個人庫存\n/my - 查看個人庫存\n/check - 監控清單掃描\n/scan - 全市場選股掃描\n/export 代碼 - 匯出資料\n/morning - 晨間美股與台指期夜盤\n/noon - 台股收盤與台指期日盤\n/tw_market - 台股收盤與台指期日盤\n/stock_chart 代碼 起日 迄日 頻率 - 匯出個股互動圖表\n/tmf_chart 起日 迄日 盤別 頻率 - 匯出 TMF 互動圖表")

async def list_stocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    stocks = get_monitor_stocks(config)
    stock_lines = [format_stock_display(stock) for stock in stocks]
    msg = "📊 目前監控清單：\n" + ("\n".join(stock_lines) if stock_lines else "清單為空")
    await safe_send_reply(update, msg)

async def add_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    stock = get_canonical_stock_symbol(context.args[0])
    stock_name = " ".join(context.args[1:]).strip() or get_official_stock_name(stock)
    config = load_config()
    if find_stock_index(config.get('monitor_stocks', []), stock) == -1:
        entry = {'symbol': stock, 'name': stock_name} if stock_name else stock
        config.setdefault('monitor_stocks', []).append(entry)
        save_config(config)
        await safe_send_reply(update, f"✅ 已加入：{format_stock_display({'symbol': stock, 'name': stock_name})}")

async def del_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    stock = context.args[0].upper()
    config = load_config()
    stock_index = find_stock_index(config.get('monitor_stocks', []), stock)
    if stock_index != -1:
        removed_stock = normalize_stock_entry(config['monitor_stocks'][stock_index])
        config['monitor_stocks'].pop(stock_index)
        save_config(config)
        await safe_send_reply(update, f"🗑️ 已從清單刪除：{format_stock_display(removed_stock)}")


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
    await safe_send_reply(update, "🔎 正在執行策略掃描，請稍候...")
    config = load_config()
    final_signals = []
    for stock in get_monitor_stocks(config):
        # 策略 A
        res_a = check_signal(stock)
        if res_a: final_signals.append(res_a)
        # 策略 B（回測突破）暫時停用
        # res_b = check_advanced_signal(stock)
        # if res_b: final_signals.append(res_b)
        # 策略 C
        res_c = check_ma105_signal(stock)
        if res_c: final_signals.append(res_c)
    
    msg = "\n\n".join(final_signals) if final_signals else "📭 目前無突破訊號。"
    await safe_send_reply(update, msg)

def build_scan_strategy_keyboard() -> InlineKeyboardMarkup:
    # 新增 /scan 的互動式按鈕選單，避免使用者每次輸入指令就直接執行所有高成本掃描。
    rows = [
        [InlineKeyboardButton("1. 財報營收選股", callback_data=f"{SCAN_CALLBACK_PREFIX}1")],
        [InlineKeyboardButton("2. 60 日法人動態選股", callback_data=f"{SCAN_CALLBACK_PREFIX}2")],
        [InlineKeyboardButton("3. 投信認養股", callback_data=f"{SCAN_CALLBACK_PREFIX}3")],
        [InlineKeyboardButton("4. 法人持股比例增加", callback_data=f"{SCAN_CALLBACK_PREFIX}4")],
        [InlineKeyboardButton("5. 每週大戶持股選股", callback_data=f"{SCAN_CALLBACK_PREFIX}5")],
        [InlineKeyboardButton("6. 全部執行", callback_data=f"{SCAN_CALLBACK_PREFIX}6")],
    ]
    return InlineKeyboardMarkup(rows)


async def run_selected_scan_reports(update: Update, selection: str):
    selected_keys = SCAN_SELECTIONS.get(selection)
    if not selected_keys:
        await safe_send_reply(update, "❌ 無效的選股策略選項。")
        return

    # 新增執行器：把財報掃描與籌碼掃描拆開執行，全部執行時共用一次籌碼資料上下文，減少重複抓取。
    try:
        financial_report = None
        if "financial" in selected_keys:
            config = load_config()
            financial_report = await asyncio.to_thread(
                run_tw_market_scan,
                False,
                None,
                config.get("scan_settings", {}),
            )

        chip_keys = [key for key in selected_keys if key.startswith("chip_")]
        chip_reports = {}
        if chip_keys:
            chip_reports, _ = await asyncio.to_thread(build_chip_reports, chip_keys, False, get_tw_today())
    except Exception as exc:
        print(f"❌ /scan 選單執行失敗: {exc}")
        await safe_send_reply(update, "❌ 選股掃描失敗，請稍後再試。")
        return

    if financial_report:
        await safe_send_reply(update, financial_report)

    for key in chip_keys:
        await safe_send_reply(update, chip_reports[key])


async def run_tw_stock_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 新增 /scan 互動流程：先讓使用者選策略，再由 callback handler 執行對應掃描。
    await safe_send_reply(update, SCAN_MENU_TEXT, reply_markup=build_scan_strategy_keyboard())


async def handle_scan_strategy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    selection = query.data.replace(SCAN_CALLBACK_PREFIX, "", 1)
    selected_keys = SCAN_SELECTIONS.get(selection)
    if not selected_keys:
        await query.edit_message_text("❌ 無效的選股策略選項。")
        return

    menu_label = SCAN_MENU_LABELS[selection]

    # 新增 callback 狀態提示：先更新選單訊息，讓使用者知道系統已接受操作並開始計算。
    await query.edit_message_text(f"已選擇：{menu_label}\n開始執行，請稍候...")
    await run_selected_scan_reports(update, selection)

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
        caption=f"📁 {display_name} 匯出完成",
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

    # 新增傳送方式：以記憶體中的 BytesIO 直接回傳 HTML 文件，避免伺服器留下多餘暫存檔。
    await update.message.reply_document(
        document=telegram.InputFile(html_buffer, filename=filename),
        caption=(
            f"📊 {meta.display_name} {chart_request.start_date} ~ "
            f"{chart_request.end_date} {chart_request.frequency}"
        ),
    )


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
            await update.message.reply_document(
                document=telegram.InputFile(html_file, filename=html_path.name),
                caption=(
                    f"📊 TMF {chart_request.start_date} ~ {chart_request.end_date} "
                    f"{chart_request.session} {chart_request.frequency}"
                ),
            )
    finally:
        # 新增清理：送出完成後立即刪除暫存 HTML，避免暫存資料堆積。
        if html_path and html_path.exists():
            html_path.unlink(missing_ok=True)

# --- 6. 定時任務 ---
async def scheduled_daily_scan(context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    print("⏰ 執行定時任務掃描...")
    final_signals = []
    for stock in get_monitor_stocks(config):
        res_a = check_signal(stock)
        if res_a: final_signals.append(res_a)
        # 策略 B（回測突破）暫時停用
        # res_b = check_advanced_signal(stock)
        # if res_b: final_signals.append(res_b)
        res_c = check_ma105_signal(stock)
        if res_c: final_signals.append(res_c)
    msg = "⏰ **12:30 策略監測報告**\n\n" + ("\n\n".join(final_signals) if final_signals else "今日無符合條件標的。")
    try:
        await context.bot.send_message(chat_id=config['chat_id'], text=msg, parse_mode='Markdown')
        print("✅ 定時報告發送成功")
    except Exception as e:
        print(f"⚠️ 定時報告發送失敗: {e}")


# 新增每日庫存籌碼推播：17:45 讀取 portfolio.json，若官方資料未更新則以 JobQueue 延後 5 分鐘重試。
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

    try:
        report = await asyncio.to_thread(build_noon_market_report)
    except MarketSummaryError as exc:
        print(f"ℹ️ 午報略過: {exc}")
        return
    except Exception as exc:
        print(f"❌ 午報排程失敗: {exc}")
        return

    await context.bot.send_message(chat_id=config['chat_id'], text=report)


async def scheduled_daily_chip_report(context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    report_date = get_tw_today()

    try:
        # 新增每日 18:00 籌碼排程：只執行策略 1 到 3，且若最新交易日不是今天就直接略過，避免非交易日誤發。
        reports, chip_context = await asyncio.to_thread(build_chip_reports, ["chip_1", "chip_2", "chip_3"], False, report_date)
    except Exception as exc:
        print(f"❌ 每日籌碼排程失敗: {exc}")
        return

    if chip_context.latest_trading_date != report_date:
        print("ℹ️ 每日籌碼排程略過：當日無最新收盤價")
        return

    for key in ("chip_1", "chip_2", "chip_3"):
        await context.bot.send_message(chat_id=config['chat_id'], text=reports[key])


async def scheduled_weekly_chip_report(context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    report_date = get_tw_today()

    try:
        # 新增每週六 12:00 籌碼排程：策略 4 使用最近一次集保快照與本地週快取來判斷趨勢。
        reports, chip_context = await asyncio.to_thread(build_chip_reports, ["chip_4"], False, report_date)
    except Exception as exc:
        print(f"❌ 每週大戶排程失敗: {exc}")
        return

    latest_snapshot = latest_weekly_snapshot_date(chip_context)
    if latest_snapshot is None:
        print("ℹ️ 每週大戶排程略過：尚未取得集保快照")
        return

    await context.bot.send_message(chat_id=config['chat_id'], text=reports["chip_4"])
    mark_weekly_report_sent(report_date, latest_snapshot)

# --- 7. 啟動後初始掃描 ---
async def run_post_init_scan(context: ContextTypes.DEFAULT_TYPE):
    application = context.application
    config = load_config()
    print("🚀 執行啟動初始掃描...")
    final_signals = []
    for stock in get_monitor_stocks(config):
        res_a = check_signal(stock)
        if res_a: final_signals.append(res_a)
        # 策略 B（回測突破）暫時停用
        # res_b = check_advanced_signal(stock)
        # if res_b: final_signals.append(res_b)
        res_c = check_ma105_signal(stock)
        if res_c: final_signals.append(res_c)
    init_msg = "🤖 機器人上線！\n\n【啟動掃描結果】\n" + ("\n\n".join(final_signals) if final_signals else "目前無突破訊號。")
    try:
        await application.bot.send_message(chat_id=config['chat_id'], text=init_msg)
        print("✅ 初始通知發送成功")
    except Exception as e:
        print(f"⚠️ 初始通知發送失敗: {e}")


# 新增啟動晨報檢查：只在台北時間 06:00 到 09:00 之間主動推播一次晨間市場摘要。
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


async def run_startup_weekly_chip_fallback_if_needed(context: ContextTypes.DEFAULT_TYPE):
    report_date = get_tw_today()
    if not should_run_startup_weekly_fallback(report_date):
        return

    config = load_config()
    try:
        # 新增週報補發：若週六排程因維護或重啟未執行，週一啟動時自動檢查並補送一次策略 4。
        reports, chip_context = await asyncio.to_thread(build_chip_reports, ["chip_4"], False, report_date)
    except Exception as exc:
        print(f"❌ 啟動週報補發失敗: {exc}")
        return

    latest_snapshot = latest_weekly_snapshot_date(chip_context)
    if latest_snapshot is None:
        print("ℹ️ 啟動週報補發略過：尚未取得集保快照")
        return

    try:
        await context.application.bot.send_message(chat_id=config['chat_id'], text=reports["chip_4"])
        mark_weekly_report_sent(report_date, latest_snapshot, fallback=True)
        print("✅ 啟動週報補發成功")
    except Exception as exc:
        print(f"⚠️ 啟動週報補發發送失敗: {exc}")


async def post_init(application):
    if application.job_queue:
        # 新增啟動任務：與原本啟動掃描分離，避免市場摘要失敗時影響既有策略掃描。
        application.job_queue.run_once(run_startup_morning_report_if_needed, when=0)
        application.job_queue.run_once(run_startup_weekly_chip_fallback_if_needed, when=0)

# --- 8. 主程式入口 ---
def main():
    config = load_config()
    # 強化 Timeout 設定
    req = HTTPXRequest(connect_timeout=60, read_timeout=60, write_timeout=60, pool_timeout=60)
    tw_tz = pytz.timezone('Asia/Taipei')

    app = ApplicationBuilder().token(config['api_token']).request(req).post_init(post_init).build()

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
        # 新增每日 18:00 籌碼選股排程，獨立於既有監控與庫存推播，避免訊息格式混雜。
        app.job_queue.run_daily(
            scheduled_daily_chip_report,
            time=time(hour=18, minute=0, tzinfo=tw_tz),
        )
        # 新增每週六 12:00 大戶持股排程，對應策略 4 的週資料節奏。
        app.job_queue.run_daily(
            scheduled_weekly_chip_report,
            time=time(hour=12, minute=0, tzinfo=tw_tz),
            days=(5,),
        )

    # 註冊指令
    app.add_handler(CommandHandler("start", start))
    # 新增策略監控指令命名：正式以 *_m 區隔監控名單與個人庫存名單。
    app.add_handler(CommandHandler("list_m", list_monitor_stocks))
    app.add_handler(CommandHandler("add_m", add_monitor_stock))
    app.add_handler(CommandHandler("del_m", del_monitor_stock))
    # 新增個人庫存指令：/in、/out、/my 全數委派給 portfolio_manager.py。
    app.add_handler(CommandHandler("in", add_portfolio_command))
    app.add_handler(CommandHandler("out", remove_portfolio_command))
    app.add_handler(CommandHandler("my", list_portfolio_command))
    app.add_handler(CommandHandler("check", run_scan))
    app.add_handler(CommandHandler("scan", run_tw_stock_scan))
    app.add_handler(CallbackQueryHandler(handle_scan_strategy_callback, pattern=f"^{SCAN_CALLBACK_PREFIX}"))
    app.add_handler(CommandHandler("export", export_stock))
    # 新增市場摘要指令：/morning 查晨報，/noon 與 /tw_market 共用午報處理器。
    app.add_handler(CommandHandler("morning", morning_market_summary_command))
    app.add_handler(CommandHandler("noon", noon_market_summary_command))
    app.add_handler(CommandHandler("tw_market", noon_market_summary_command))
    # 新增指令註冊：支援 /stock_chart 生成台股個股的互動式 HTML 技術分析圖表。
    app.add_handler(CommandHandler("stock_chart", export_stock_chart))
    # 新增指令註冊：支援 /tmf_chart 生成 TMF 的互動式 Lightweight Charts HTML 報表。
    app.add_handler(CommandHandler("tmf_chart", export_tmf_chart))

    print("🚀 策略機器人啟動中，定時設定：12:30 監控掃描、13:50 午報、17:45 庫存推播、18:00 籌碼掃描、週六 12:00 大戶週報...")
    app.run_polling(bootstrap_retries=-1)

if __name__ == "__main__":
    main()
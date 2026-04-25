import yfinance as yf
import pandas as pd
import json
import asyncio
import telegram
import httpx
import pytz
from datetime import time, datetime, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

from data_fetcher import StockExportError, StockNotFoundError
from export_service import build_stock_export_workbook
from stock_scanner import run_scan as run_tw_market_scan

OFFICIAL_NAME_CACHE = {}
OFFICIAL_SYMBOL_CACHE = {}
OFFICIAL_NAME_CACHE_EXPIRES_AT = None
OFFICIAL_NAME_CACHE_TTL = timedelta(hours=12)
TWSE_NAME_API_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_NAME_API_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"

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
async def safe_send_reply(update: Update, text: str):
    """具備自動重試的發送函式"""
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
                await update.message.reply_text(chunk)
                sent = True
                break
            except Exception as e:
                print(f"⚠️ 第 {i+1} 次發送失敗: {e}")
                await asyncio.sleep(3)
        if not sent:
            print("❌ 訊息發送失敗，已放棄本段回覆")
            return

# --- 5. 指令處理器 ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send_reply(update, "🤖 策略機器人已就緒！\n/list - 查看清單\n/check - 監控清單掃描\n/scan - 全市場選股掃描\n/add 代碼 名稱 - 加入\n/del 代碼 - 刪除\n/export 代碼 - 匯出資料")

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

async def run_tw_stock_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send_reply(update, "🔍 正在執行全市場選股掃描，這會花一些時間，請稍候...")
    config = load_config()
    scan_settings = config.get('scan_settings', {})
    try:
        report = await asyncio.to_thread(run_tw_market_scan, False, None, scan_settings)
    except Exception as exc:
        print(f"❌ /scan 執行失敗: {exc}")
        await safe_send_reply(update, "❌ 全市場掃描失敗，請稍後再試。")
        return

    await safe_send_reply(update, report)

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


async def post_init(application):
    if application.job_queue:
        application.job_queue.run_once(run_post_init_scan, when=0)

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

    # 註冊指令
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_stocks))
    app.add_handler(CommandHandler("add", add_stock))
    app.add_handler(CommandHandler("del", del_stock))
    app.add_handler(CommandHandler("check", run_scan))
    app.add_handler(CommandHandler("scan", run_tw_stock_scan))
    app.add_handler(CommandHandler("export", export_stock))

    print("🚀 策略機器人啟動中，定時設定：每日 12:30...")
    app.run_polling(bootstrap_retries=-1)

if __name__ == "__main__":
    main()
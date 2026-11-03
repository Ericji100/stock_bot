"""Shared dated official closing snapshots, not an intraday or history substitute."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
from threading import RLock

import httpx

ROOT_DIR = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT_DIR / '.cache' / 'official_daily_quotes'
ENDPOINTS = {
    'TWSE': 'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
    'TPEX': 'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes',
}
_LOCK = RLock()


def _number(value):
    try:
        number = float(str(value).replace(',', '').strip())
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def parse_official_closes(rows, market, target_date):
    result = {}
    for row in rows if isinstance(rows, list) else []:
        raw_date = str(row.get('Date') or '').strip().replace('/', '').replace('-', '')
        try:
            year = int(raw_date[:-4]) + (1911 if len(raw_date) == 7 else 0)
            day = date(year, int(raw_date[-4:-2]), int(raw_date[-2:]))
        except (ValueError, TypeError):
            continue
        if day > target_date:
            continue
        code = str(row.get('Code') if market == 'TWSE' else row.get('SecuritiesCompanyCode') or '').strip()
        price = _number(row.get('ClosingPrice') if market == 'TWSE' else row.get('Close'))
        change = _number(row.get('Change'))
        volume = _number(row.get('TradeVolume') if market == 'TWSE' else row.get('TradingShares'))
        if not code or price is None or price <= 0 or volume is None:
            continue
        previous = price - change if change is not None else None
        result[code + ('.TW' if market == 'TWSE' else '.TWO')] = {
            'price': price, 'price_date': day.isoformat(), 'previous_close': previous,
            'change_pct': round(change / previous * 100, 2) if previous and previous > 0 else None,
            'volume': volume / 1000,
            'turnover': (amount / 1000 if (amount := _number(row.get('TradeValue') if market == 'TWSE' else row.get('TransactionAmount'))) is not None else None),
            'price_source': ENDPOINTS[market], 'price_basis': 'official_unadjusted_close',
        }
    return result


def load_official_closes(universe, target_date):
    # Latest-only APIs cannot reconstruct an older as-of date.
    if target_date < datetime.now().date() - timedelta(days=1):
        return {}
    markets = {'TPEX' if str(entry.symbol).endswith('.TWO') else 'TWSE' for entry in universe}
    result = {}
    for market in sorted(markets):
        path = CACHE_DIR / (market.lower() + '.json')
        try:
            with _LOCK:
                cached = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
                age = datetime.now().timestamp() - path.stat().st_mtime if path.exists() else float('inf')
                if not 0 <= age < 1800:
                    response = httpx.get(ENDPOINTS[market], timeout=15, follow_redirects=True)
                    response.raise_for_status()
                    cached = {'rows': response.json(), 'fetched_at': datetime.now().isoformat()}
                    # Validate before caching a blocked/malformed response.
                    if not isinstance(cached['rows'], list) or not cached['rows']:
                        continue
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(cached, ensure_ascii=False), encoding='utf-8')
            result.update(parse_official_closes(cached.get('rows'), market, target_date))
        except (httpx.HTTPError, ValueError, OSError):
            continue
    return {entry.symbol: result[entry.symbol] for entry in universe if entry.symbol in result}


def merge_official_closes(metrics, official):
    merged = {key: dict(value) for key, value in metrics.items()}
    for symbol, quote in official.items():
        old = merged.get(symbol) or {}
        if old.get('price_date', '') > quote['price_date']:
            continue
        if old and old.get('price_date') != quote['price_date']:
            # New close is known; intervening bars and ex-right adjustments are not.
            value = {'historical_metric_snapshot': old, 'history_metrics_as_of': old.get('price_date'),
                     'history_refresh_required': True}
        else:
            value = dict(old)
        merged[symbol] = {**value, **quote}
    return merged

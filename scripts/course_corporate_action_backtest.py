"""Corporate-action-aware replay of the unchanged v2 fixed-defense strategy.

Signals use an as-of affine-adjusted history. Execution and wealth use actual
price units, explicit share entitlements and non-reinvested cash distributions.
Old v2 files, production caches and trading rules remain untouched.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date
from dataclasses import asdict
from functools import lru_cache
import math
import re
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.course_corporate_action_data import RUN, SOURCE, AS_OF, read, save, digest, yahoo_events
from scripts.course_daily_screen_trial import THRESHOLDS, _add_indicators
from scripts.course_watchlist_backtest import enlightenment_snapshot
from scripts import course_radar_trigger_backtest as legacy


def transform_price(price, action):
    subscription = action.get('capital_issue_ratio', 0.)
    return (float(price) - action["cash"] + subscription*action.get('subscription_price', 0.)) / (action["ratio"]+subscription)


def restore_raw(frame, events, split_adjusted=True):
    """Undo Yahoo's future split normalization; do not add or remove dividends."""
    raw = frame.copy()
    if split_adjusted:
        for event in events:
            if event["ratio"] != 1:
                mask = raw.date.dt.strftime("%Y-%m-%d") < event["date"]
                raw.loc[mask, ["open", "high", "low", "close"]] *= event["ratio"]
    raw[["open", "high", "low", "close"]] = raw[["open", "high", "low", "close"]].round(4)
    return raw


def adjusted_history(raw, events, day):
    """No action later than day may enter the analysis, even if now known."""
    history = raw[raw.date.dt.strftime("%Y-%m-%d") <= day].copy()
    for event in events:
        if event["date"] > day: continue
        mask = history.date.dt.strftime("%Y-%m-%d") < event["date"]
        subscription = event.get('capital_issue_ratio', 0.)
        history.loc[mask, ["open", "high", "low", "close"]] = (
            history.loc[mask, ["open", "high", "low", "close"]] - event["cash"]
            + subscription*event.get('subscription_price', 0.)
        ) / (event["ratio"]+subscription)
        # Equivalent current share units across genuine share-count changes.
        history.loc[mask, "volume"] *= event["ratio"]
    if (history[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("affine-adjusted historical prices nonpositive; requires a different history window")
    return _add_indicators(history)


def finmind_rows(code, dataset):
    path = RUN / "sources/finmind" / f"{code}_{dataset}.json"
    return read(path)["response"]["data"] if path.exists() else None


@lru_cache(maxsize=1)
def exchange_references():
    """Immutable public exchange snapshots, independent of FinMind quota."""
    records = {}
    for filename, market in [('twse_exrights_full.json', 'TWSE'), ('tpex_exrights_full.json', 'TPEX')]:
        path = RUN/'sources'/filename
        if not path.exists(): continue
        source = read(path)
        response = source['response']
        rows = response['data'] if market == 'TWSE' else response['tables'][0]['data']
        for row in rows:
            yy, mm, dd = map(int, re.findall(r'\d+', row[0]))
            day = f'{yy+1911:04d}-{mm:02d}-{dd:02d}'
            if day > AS_OF: continue
            number = lambda i: float(str(row[i]).replace(',', ''))
            kind = row[6] if market == 'TWSE' else row[8].replace('除', '')
            r = {'date': day, 'before_price': number(3), 'after_price': number(4),
                 'stock_or_cache_dividend': kind, 'source': source['url'], 'market': market}
            if market == 'TWSE' and kind == '息': r['cash'] = number(5)
            if market == 'TPEX':
                r.update(cash=number(13), ratio=1+number(14)/1000,
                         issued_shares=number(15), subscription_price=number(16))
                if number(15) and number(19) and number(20):
                    r['capital_issue_ratio'] = number(15)*number(20)/1000/number(19)
            detail = RUN/'sources/twse_details'/f'{str(row[1]).strip()}_{day}.json'
            if market == 'TWSE' and detail.exists():
                dr = read(detail)['response']['data'][0]
                n = lambda i: float(re.search(r'[\d,.]+',dr[i]).group().replace(',',''))
                r.update(cash=n(2), ratio=1+n(4)/1000, issued_shares=n(6), subscription_price=n(7))
                if n(6) and n(10) and n(11):
                    r['capital_issue_ratio'] = n(6)*n(11)/1000/n(10)
                r['detail_source'] = read(detail)['url']
            records.setdefault(str(row[1]).strip(), []).append(r)
    return records


def load_inputs(item):
    path = Path(item["frozen_price_path"])
    assert digest(path) == item["price_sha256"]
    frame = pd.read_csv(path)
    frame["date"] = pd.to_datetime(frame.date)
    frame = frame[frame.date.dt.strftime("%Y-%m-%d") <= AS_OF].sort_values('date').reset_index(drop=True)
    zero_bars = list(frame.loc[frame.volume <= 0, 'date'].dt.strftime('%Y-%m-%d'))
    frame = frame[frame.volume > 0].reset_index(drop=True)
    frame["volume"] = frame.volume.astype(float)
    yahoo = yahoo_events(item["code"])
    raw = restore_raw(frame, yahoo, "adj_close" in frame.columns)
    policy = finmind_rows(item["code"], "TaiwanStockDividend")
    results = finmind_rows(item["code"], "TaiwanStockDividendResult")
    prices = finmind_rows(item["code"], "TaiwanStockPrice")
    events = {e["date"]: {**e, "source": "Yahoo corporate actions", "pay_date": None,
                         "kind": "DIVIDEND_AND_SHARES", "stock_release_date": None} for e in yahoo}
    quality = {"yahoo_events": len(yahoo), "policy_available": policy is not None,
               "official_price_available": prices is not None, "zero_volume_dates_removed": zero_bars, "notes": []}
    trading_days = list(raw.date.dt.strftime('%Y-%m-%d'))
    effective = lambda d: next((t for t in trading_days if t >= d), d) if d >= trading_days[0] else d
    if policy is not None:
        cash_by_day, stock_by_day = {}, {}
        for p in policy:
            announced = p.get("AnnouncementDate", "")
            for field in ("CashExDividendTradingDate", "StockExDividendTradingDate"):
                day = p.get(field)
                if not day or day > AS_OF or day < '2025-07-01': continue
                day = effective(day)
                if announced and announced > day:
                    quality["notes"].append(f"{day}: policy revised after ex-date; contemporaneous reference result required")
                if field.startswith("Cash"):
                    value = float(p.get("CashEarningsDistribution", 0)) + float(p.get("CashStatutorySurplus", 0))
                    if value:
                        cash_by_day[day] = (value, p.get("CashDividendPaymentDate") or None)
                else:
                    value = float(p.get("StockEarningsDistribution", 0)) + float(p.get("StockStatutorySurplus", 0))
                    if value: stock_by_day[day] = 1 + value / 10
                    capital = float(p.get('TotalNumberOfCashCapitalIncrease', 0))
                    prior_shares = float(p.get('ParticipateDistributionOfTotalShares', 0))
                    if capital and prior_shares:
                        e = events.setdefault(day, {'date': day, 'cash': 0., 'ratio': 1., 'pay_date': None,
                                                   'stock_release_date': None, 'kind': 'CAPITAL_ISSUE'})
                        e['capital_issue_ratio'] = capital/prior_shares
                        e['subscription_price'] = float(p.get('CashIncreaseSubscriptionpRrice', 0))
        for day, (value, payment) in cash_by_day.items():
            e = events.setdefault(day, {"date": day, "cash": 0., "ratio": 1., "stock_release_date": None})
            if abs(e['cash']-value) > .005:
                quality["notes"].append(f"{day}: cash corrected {e['cash']} -> {value}")
            e.update(cash=value, pay_date=payment, source="FinMind dividend policy + Yahoo shares", kind="DIVIDEND_AND_SHARES")
        # Actual ex-right dates in company policies replace Yahoo's bonus dates.
        # Do not replace genuine splits/par-value changes/reductions with policy /10 estimates.
        for day, ratio in stock_by_day.items():
            e = events.setdefault(day, {"date": day, "cash": 0., "ratio": 1., "pay_date": None,
                                       "stock_release_date": None, "source": "FinMind policy", "kind": "DIVIDEND_AND_SHARES"})
            if e["ratio"] == 1 or abs(e["ratio"]-ratio) < .005:
                e["ratio"] = ratio
    reference = {effective(r["date"]): r for r in (results or []) if r['date'] >= trading_days[0]}
    official = exchange_references().get(item['code'], [])
    for r in official:
        day = effective(r['date'])
        if r['date'] >= trading_days[0]: reference[day] = r
        e = events.setdefault(day, {'date': day, 'cash': 0., 'ratio': 1., 'pay_date': None,
                                  'stock_release_date': None, 'kind': 'DIVIDEND_AND_SHARES'})
        if 'cash' in r: e['cash'] = r['cash']
        if 'ratio' in r:
            e['ratio'] = r['ratio']
            if r.get('capital_issue_ratio'):
                e.update(capital_issue_ratio=r['capital_issue_ratio'], subscription_price=r['subscription_price'])
            if r['issued_shares'] and not e.get('capital_issue_ratio'):
                # Reference prices are rounded: identify rather than silently
                # infer an imprecise subscription ratio from them.
                quality['notes'].append(f"{day}: capital issue terms need explicit source")
                e['unresolved_capital_issue'] = True
        e['source'] = r['source']
        if 'detail_source' in r: e['detail_source'] = r['detail_source']
        e['official_kind'] = r['stock_or_cache_dividend']
    quality['exchange_reference_count'] = len(reference)
    for day, r in reference.items():
        if r['stock_or_cache_dividend'] == '息' and day in events and events[day]['ratio'] != 1:
            quality['notes'].append(f"{day}: removed Yahoo phantom share event; exchange classifies cash only")
            events[day]['ratio'] = 1.
    # Raw quote history itself can contain rights adjustment or duplicated Yahoo
    # split factors. Reconcile backwards against actual pre-ex market closes.
    for day, r in sorted(reference.items(), reverse=True):
        prior = raw.index[raw.date.dt.strftime('%Y-%m-%d') < day]
        if len(prior):
            seen = float(raw.at[prior[-1], 'close'])
            scale = float(r['before_price']) / seen
            if abs(scale-1) > .0001:
                raw.loc[prior, ['open','high','low','close']] *= scale
                quality['notes'].append(f"{day}: raw quote basis reconciled x{scale:.8f}")
    # For ordinary bonus issues, validate effective ratio against exchange reference prices.
    for day, event in events.items():
        r = reference.get(day)
        if r:
            expected = transform_price(float(r["before_price"]), event)
            event["reference_price"] = float(r["after_price"])
            event["reference_before"] = float(r["before_price"])
            event["reference_error"] = expected - float(r["after_price"])
            if abs(event["reference_error"]) > max(.06, float(r["before_price"])*.001):
                quality["notes"].append(f"{day}: reference mismatch {event['reference_error']:.6f}")
        if event["ratio"] < 1:
            event["kind"] = "CAPITAL_REDUCTION"
            quality["notes"].append(f"{day}: capital reduction requires specific validation")
    if prices:
        actual = {p["date"]: p for p in prices}
        differences = []
        for i, row in raw.iterrows():
            p = actual.get(row.date.date().isoformat())
            if not p: continue
            differences.append(abs(row.close-float(p['close'])))
            # Correct real execution prices only; do not replace the original volume feed.
            for key, src in (("open", "open"), ("high", "max"), ("low", "min"), ("close", "close")):
                raw.at[i, key] = float(p[src])
        quality["raw_close_max_correction"] = max(differences, default=0)
        quality["raw_bars_checked"] = len(differences)
    # Optional independently sourced corrections and share-listing dates.
    overrides_path = RUN / "action_overrides.json"
    overrides = read(overrides_path).get(item["code"], []) if overrides_path.exists() else []
    for override in overrides:
        day = override["date"]
        if override.get("remove"):
            events.pop(day, None)
        else:
            events.setdefault(day, {"date": day, "cash": 0., "ratio": 1., "pay_date": None,
                                   "stock_release_date": None, "kind": "DIVIDEND_AND_SHARES"}).update(override)
    return raw, sorted(events.values(), key=lambda e: e["date"]), quality


def event_dates(events, raw):
    days = list(raw.date.dt.strftime("%Y-%m-%d"))
    mapped = {}
    for e in events:
        # Suspended/non-trading ex dates apply at the first available following bar.
        day = next((d for d in days if d >= e['date']), None)
        if day and day > days[0]: mapped.setdefault(day, []).append(e)
    return mapped


def book_action(trade, event):
    q = trade["units"]
    cash = q * event["cash"]
    trade["cash_entitlements"].append({"ex_date": event["date"], "per_share": event["cash"],
        "units": q, "amount": cash, "pay_date": event.get("pay_date"), "kind": event.get('kind')}) if cash else None
    trade["units"] *= event["ratio"]
    trade["defense"] = transform_price(trade["defense"], event)
    trade["action_events"].append({**event, "units_before": q, "units_after": trade["units"],
                                   "new_defense": trade["defense"]})


def wealth(trade, price):
    return trade["units"] * price + sum(c["amount"] for c in trade["cash_entitlements"])


def open_trade(code, name, day, price, defense, plan, episode):
    return {"trade_id": f"{code}-{day}-{episode['sequence']}", "code": code, "name": name,
        "entry_date": day, "entry_price": float(price), "initial_defense": float(defense),
        "defense": float(defense), "armed_date": plan["armed_date"], "plan": plan,
        "episode_sequence": episode["sequence"], "monitor_added_date": episode["added_date"],
        "units": 1., "cash_entitlements": [], "action_events": [], "status": "OPEN",
        "exit_date": None, "exit_price": None, "exit_trigger_date": None,
        "max_wealth": float(price), "mfe_date": day, "daily_wealth": []}


def finish_trade(t, day, price, closed=False):
    t["performance_date"], t["performance_price"] = day, float(price)
    t["performance_return_pct"] = (wealth(t, price) / t["entry_price"] - 1) * 100
    t["cash_entitlement_per_original_share"] = sum(c['amount'] for c in t['cash_entitlements'])
    t["mfe_pct"] = (t['max_wealth']/t['entry_price']-1)*100
    t["status_label"] = legacy.status_label(t["status"])
    if closed:
        t["exit_date"], t["exit_price"] = day, float(price)


def replay_stock(item, selections, raw, actions, fixed_trade=None):
    mapped = event_dates(actions, raw)
    selected = {r['date'].isoformat(): r for r in selections}
    start_day = min(selected) if fixed_trade is None else fixed_trade['entry_date']
    episode, pending, current = None, None, None
    sequence = armed_count = 0
    trades, audit = [], []
    history_cache = {}
    for index, row in raw.iterrows():
        day = row.date.date().isoformat()
        if day < start_day: continue
        for event in mapped.get(day, []):
            if current: book_action(current, event)
            if pending:
                for key in ('trigger', 'chase_cap', 'defense'):
                    pending[key] = transform_price(pending[key], event)
        if current and current['status'] == 'EXIT_TRIGGERED':
            current['status'] = 'CLOSED'
            finish_trade(current, day, row.open, True)
            trades.append(current)
            current, episode, pending = None, None, None
        if fixed_trade is not None and day == fixed_trade['entry_date']:
            if current: raise ValueError('fixed entry overlaps active trade')
            # Old prices were in Yahoo split-normalized units; restore entry-day units.
            frozen = pd.read_csv(item['frozen_price_path'])
            old_close = float(frozen.loc[frozen.date == day, 'close'].iloc[0])
            factor = row.close / old_close
            price, defense = fixed_trade['entry_price'] * factor, fixed_trade['defense'] * factor
            episode = {'sequence': fixed_trade['episode_sequence'], 'added_date': fixed_trade['monitor_added_date']}
            pending = {'armed_date': fixed_trade['armed_date']}
            current = open_trade(item['code'], item['name'], day, price, defense, pending, episode)
            pending = None
        elif fixed_trade is None and not current and episode and pending:
            trigger, cap, defense = (pending[k] for k in ('trigger', 'chase_cap', 'defense'))
            if row.open <= cap and row.open > defense and trigger <= cap and row.high >= trigger:
                current = open_trade(item['code'], item['name'], day, max(row.open, trigger), defense, pending, episode)
            pending = None
        if current:
            high_wealth = wealth(current, row.high)
            if high_wealth > current['max_wealth']:
                current['max_wealth'], current['mfe_date'] = high_wealth, day
            current['daily_wealth'].append({'date': day, 'close_wealth': wealth(current, row.close),
                'defense': current['defense'], 'units': current['units'], 'close': row.close})
            if row.close < current['defense']:
                current['status'] = 'EXIT_TRIGGERED'
                current['exit_trigger_date'] = day
                current['exit_trigger_close'] = float(row.close)
                current['exit_trigger_return_pct'] = (wealth(current, row.close)/current['entry_price']-1)*100
            finish_trade(current, day, row.close)
            continue
        if fixed_trade is not None: continue
        selection = selected.get(day)
        if selection:
            if episode is None:
                sequence += 1
                episode = legacy._new_episode(selection, row.date.date(), index, sequence)
            else: legacy._merge_selection(episode, selection, row.date.date(), index)
        if episode is None: continue
        if index - episode['last_selected_position'] > 20:
            episode, pending = None, None
            continue
        # Rolling indicators are causal. Cache within an event epoch, then
        # truncate before passing anything to the signal classifier.
        epoch = tuple(e['date'] for e in actions if e['date'] <= day)
        if epoch not in history_cache:
            next_action = next((e['date'] for e in actions if e['date'] > day), '9999-12-31')
            epoch_end = raw.loc[raw.date.dt.strftime('%Y-%m-%d') < next_action, 'date'].iloc[-1].date().isoformat()
            history_cache[epoch] = adjusted_history(raw, actions, epoch_end)
        history = history_cache[epoch].loc[lambda f: f.date <= row.date].copy()
        snapshot = enlightenment_snapshot(history, THRESHOLDS)
        if snapshot['structural_invalid']:
            episode, pending = None, None
            continue
        if snapshot['armed']:
            pending = {'armed_date': day, 'trigger': float(snapshot['setup']['trigger_price']),
                'chase_cap': float(snapshot['risk']['chase_cap']), 'defense': float(snapshot['risk']['defense']),
                'pattern': snapshot['setup']['pattern_code'], 'structural_lens': snapshot['structural_lens'],
                'taiji_sequence': snapshot['taiji']['sequence'], 'taiji_state': snapshot['taiji']['state']}
            armed_count += 1
            audit.append({'date': day, **pending})
    if current: trades.append(current)
    return {'code': item['code'], 'name': item['name'], 'trades': trades, 'armed_count': armed_count, 'plans': audit}


def replay_job(job):
    item, selection, prior = job
    raw, events, check = load_inputs(item)
    result = replay_stock(item, selection, raw, events)
    fixed = []
    for trade in prior:
        fixed += replay_stock(item, selection, raw, events, trade)['trades']
    return result, fixed, {'code': item['code'], **check, 'events': events}


def frozen_selections():
    path=RUN/'selection_snapshot.json'
    if not path.exists():
        by_day,coverage=legacy.discover_radar_union(date(2026,5,21),date(2026,9,3))
        rows=[{**r,'date':r['date'].isoformat()} for group in by_day.values() for r in group]
        save(path,{'rows':rows,'coverage':coverage})
    snapshot=read(path)
    records=[{**r,'date':date.fromisoformat(r['date'])}for r in snapshot['rows']]
    return legacy._index_selections(records),snapshot['coverage']


def build():
    manifest, old = read(RUN/'input_manifest.json'), read(SOURCE)
    assert digest(SOURCE) == manifest['source_sha256']
    assert all(old['parameters'][key] == value for key,value in asdict(THRESHOLDS).items())
    selections, coverage = frozen_selections()
    assert len(selections) == 695 and sum(map(len, selections.values())) == 8467
    results, fixed, errors, quality = [], [], [], []
    old_by_code = {c['code']: c for c in old['candidates']}
    with ProcessPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(replay_job,(item,selections[item['code']],old_by_code[item['code']]['trades'])):item['code']
                for item in manifest['items']}
        for n, future in enumerate(as_completed(jobs),1):
            try:
                result, prior, check = future.result()
                results.append(result); fixed.extend(prior); quality.append(check)
            except Exception as exc:
                errors.append({'code':jobs[future], 'error':str(exc)})
            if n % 25 == 0 or n == len(jobs):
                print(f"Replay {n}/695 trades={sum(len(r['trades']) for r in results)} errors={len(errors)}",flush=True)
    results.sort(key=lambda r:r['code']); fixed.sort(key=lambda t:(t['entry_date'],t['trade_id']))
    quality.sort(key=lambda q:q['code'])
    save(RUN/'data_quality.json', quality)
    save(RUN/'backtest.json', {'method_version': 'v3-affine-corporate-actions-fixed-defense', 'as_of': AS_OF,
        'parameters': asdict(THRESHOLDS), 'candidates': results, 'fixed_original_entries': fixed,
        'errors': errors, 'source_coverage': coverage,
        'notes': ['Gross economic returns include cash receivables without reinvestment.',
                  'Stock entitlements are economically valued; executable ledger must separately handle listing dates.',
                  'Original volumes retained and adjusted into current share units only for analysis.']})
    print('ERRORS', errors)


if __name__ == '__main__':
    build()

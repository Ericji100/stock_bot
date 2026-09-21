"""Freeze replay inputs and fetch public corporate-action evidence, without editing bot caches."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import ssl
import sys
import time

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SOURCE = ROOT / "reports/course_backtest/2026-09-04/radar_full_history_v2/backtest.json"
RUN = ROOT / "reports/course_backtest/2026-09-05/radar_full_history_v3_corporate_actions"
AS_OF = "2026-09-04"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def fetch_yahoo(item):
    symbol, code = item["symbol"], item["code"]
    target = RUN / "sources/yahoo" / f"{code}.json"
    if target.exists(): return {"code": code, "cached": True}
    params = {"period1": int(datetime(2025, 7, 1, tzinfo=timezone.utc).timestamp()),
              "period2": int(datetime(2026, 9, 5, tzinfo=timezone.utc).timestamp()),
              "interval": "1d", "events": "div,splits", "includeAdjustedClose": "true"}
    with httpx.Client(timeout=25, headers={"User-Agent": "Mozilla/5.0"}) as client:
        response = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}", params=params)
        response.raise_for_status()
        data = response.json()
    if not data.get("chart", {}).get("result"):
        raise ValueError(f"{code}: chart unavailable")
    save(target, {"retrieved_utc": datetime.now(timezone.utc).isoformat(),
                  "url": str(response.url), "response": data})
    return {"code": code, "events": len(data['chart']['result'][0].get('events', {}))}


def freeze_and_fetch():
    original = read(SOURCE)
    manifest_path = RUN / "input_manifest.json"
    if not manifest_path.exists():
        items = []
        for c in original["candidates"]:
            destination = RUN / "sources/prices" / Path(c["source_path"]).name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists(): shutil.copy2(c["source_path"], destination)
            items.append({"code": c["code"], "name": c["name"], "symbol": c["symbol"],
                "market": c["market"], "original_price_path": c["source_path"],
                "frozen_price_path": str(destination), "price_sha256": digest(destination)})
        save(manifest_path, {"source_path": str(SOURCE), "source_sha256": digest(SOURCE),
            "as_of": AS_OF, "cohort_count": len(items), "items": items})
    items = read(manifest_path)["items"]
    errors = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_yahoo, item): item for item in items}
        for n, future in enumerate(as_completed(futures), 1):
            try: future.result()
            except Exception as exc:
                item = futures[future]
                errors.append({"code": item["code"], "error": str(exc)})
            if n % 50 == 0 or n == len(items):
                print(f"Yahoo {n}/{len(items)}, errors={len(errors)}", flush=True)
    save(RUN / "sources/fetch_errors.json", errors)
    print(json.dumps(errors, ensure_ascii=False))


def fetch_finmind(codes, datasets):
    from stock_ai_bot.data_sources.finmind_client import FinMindClient
    client = FinMindClient(allow_anonymous=True, timeout=25)
    for n, code in enumerate(codes, 1):
        for dataset in datasets:
            path = RUN / "sources/finmind" / f"{code}_{dataset}.json"
            if path.exists(): continue
            params = {"stock_id": code, "start_date": "2025-07-01"}
            if dataset != "TaiwanStockDividend": params["end_date"] = AS_OF
            data = client.request_dataset(dataset, params)
            if data.get("status") != 200:
                raise RuntimeError(f"FinMind request failed {code} {dataset}: {data.get('msg')}")
            save(path, {"dataset": dataset, "params": params,
                        "retrieved_utc": datetime.now(timezone.utc).isoformat(), "response": data})
            time.sleep(.25)
        print(f"FinMind {n}/{len(codes)} {code}", flush=True)


def yahoo_events(code):
    data = read(RUN / "sources/yahoo" / f"{code}.json")["response"]["chart"]["result"][0]
    events = {}
    for kind, rows in data.get("events", {}).items():
        for row in rows.values():
            day = datetime.fromtimestamp(row["date"], timezone.utc).date().isoformat()
            if day > AS_OF: continue
            event = events.setdefault(day, {"date": day, "cash": 0., "ratio": 1.})
            if kind == "dividends": event["cash"] += float(row["amount"])
            elif kind == "splits": event["ratio"] *= float(row["numerator"]) / float(row["denominator"])
    # Yahoo dividend amounts, like its quotes, are adjusted for later stock splits.
    for day, event in events.items():
        future = [e["ratio"] for d, e in events.items() if d > day]
        import math
        event["cash"] *= math.prod(future)
    return sorted(events.values(), key=lambda x: x["date"])


def inventory():
    items = read(RUN / "input_manifest.json")["items"]
    rows = []
    for item in items:
        try:
            events = yahoo_events(item["code"])
            rows.append({"code": item["code"], "name": item["name"], "events": events,
                         "in_window": [e for e in events if "2026-05-21" <= e["date"] <= AS_OF]})
        except Exception as exc:
            rows.append({"code": item["code"], "error": str(exc)})
    save(RUN / "event_inventory.json", rows)
    print(json.dumps({"stocks": len(rows), "errors": sum('error' in r for r in rows),
                      "window_actions": sum(len(r.get('in_window', [])) for r in rows),
                      "window_splits": sum(e['ratio'] != 1 for r in rows for e in r.get('in_window', []))}))


def fetch_exchanges():
    """Read-only public sources, cached so replay never spends API quota."""
    sources=[('twse_exrights_full.json','https://www.twse.com.tw/rwd/zh/exRight/TWT49U',
              {'response':'json','startDate':'20250701','endDate':'20260904'}),
             ('tpex_exrights_full.json','https://www.tpex.org.tw/www/bulletin/exDailyQ',
              {'response':'json','startDate':'2025/07/01','endDate':'2026/09/04'})]
    ctx=ssl.create_default_context()
    # TPEX chain lacks a strict-mode extension. Still validate certificates and
    # hostname; never turn off TLS verification.
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    with httpx.Client(verify=ctx,timeout=45,follow_redirects=True) as client:
        for filename,url,params in sources:
            path=RUN/'sources'/filename
            if path.exists():continue
            response=client.get(url,params=params);response.raise_for_status()
            data=response.json()
            if not(data.get('data') or data.get('tables')):raise ValueError(data)
            save(path,{'url':str(response.url),'retrieved_utc':datetime.now(timezone.utc).isoformat(),'response':data})
        # Detailed share / subscription terms for the TWSE rights rows that
        # have no dividend-policy coverage or do not reconcile to the reference.
        from scripts.course_corporate_action_backtest import load_inputs, exchange_references
        for item in read(RUN/'input_manifest.json')['items']:
            _,events,_=load_inputs(item)
            for e in events:
                if abs(e.get('reference_error',0))<=max(.06,e.get('reference_before',0)*.001):continue
                if not any(r['market']=='TWSE' and r['date']==e['date'] for r in exchange_references().get(item['code'],[])):continue
                path=RUN/'sources/twse_details'/f"{item['code']}_{e['date']}.json"
                if path.exists():continue
                response=client.get('https://www.twse.com.tw/rwd/zh/exRight/TWT49UDetail',
                    params={'STK_NO':item['code'],'T1':e['date'].replace('-',''),'response':'json'})
                response.raise_for_status();data=response.json()
                if str(data.get('stat','')).lower()!='ok':raise ValueError(data)
                save(path,{'url':str(response.url),'retrieved_utc':datetime.now(timezone.utc).isoformat(),'response':data})
                time.sleep(.25)
        exchange_references.cache_clear()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["fetch", "inventory", "finmind", "exchanges"])
    parser.add_argument("--codes", default="")
    parser.add_argument("--datasets", default="TaiwanStockDividend,TaiwanStockDividendResult")
    args = parser.parse_args()
    if args.command == "fetch": freeze_and_fetch()
    elif args.command == "inventory": inventory()
    elif args.command == "exchanges": fetch_exchanges()
    else: fetch_finmind(args.codes.split(','), args.datasets.split(','))

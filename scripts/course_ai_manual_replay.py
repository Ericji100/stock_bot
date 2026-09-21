"""Sequential, human/model-authored judgments; never generates an AI verdict.

Only data preparation, execution of sealed plans and accounting are automated.
Run `init`, `packet`, author decisions/<date>.json, then `advance`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.course_radar_trigger_backtest import discover_radar_union
from scripts.course_watchlist_backtest import load_full_frame, load_stock_map, resolve_stock

RUN = ROOT / "reports/course_backtest/2026-09-05/ai_replay_20260717_v1"
LABELS = {"WAIT": "FORMING（條件形成中）", "ARM": "ARMED（已建立進場計畫）",
          "HOLD": "OPEN（持有中）", "CLOSED": "CLOSED（交易已結束）"}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, obj, *, immutable=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if immutable and path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise ValueError(f"immutable artifact already exists: {path}")
        return
    path.write_text(text, encoding="utf-8")


def load_frames(manifest):
    frames = {}
    for item in manifest["cohort"]:
        if digest(item["price_path"]) != item["price_sha256"]:
            raise ValueError("source prices changed")
        frame, _ = load_full_frame(item["symbol"], date.fromisoformat(manifest["as_of"]))
        frames[item["alias"]] = frame
    return frames


def buy(q, p):
    return q * p * 1.001425


def sell(q, p):
    return q * p * (1 - .001425 - .003)


def row_for(frame, day):
    rows = frame[frame.date.dt.strftime("%Y-%m-%d") == day]
    return None if rows.empty else rows.iloc[-1]


def init_run():
    if (RUN / "manifest.json").exists():
        raise ValueError("run already initialized")
    selected = date(2026, 7, 17)
    source, coverage = discover_radar_union(selected, selected)
    rows = source[selected]
    mapping = load_stock_map()
    cohort, frames = [], {}
    for n, selection in enumerate(sorted(rows, key=lambda x: x["code"]), 1):
        stock = resolve_stock(selection["code"], selection["name"], mapping)
        if stock is None:
            raise ValueError(f"unresolved candidate {selection['code']}")
        frame, path = load_full_frame(stock["symbol"], date(2026, 9, 4))
        alias = f"S{n:02d}"
        frames[alias] = frame
        cohort.append({"alias": alias, "code": stock["code"], "name": selection["name"],
                       "symbol": stock["symbol"], "price_path": str(path), "price_sha256": digest(path),
                       "selection_sources": selection["source_paths"]})
    calendar = sorted({r.date().isoformat() for f in frames.values() for r in f.date
                       if selected <= r.date() <= date(2026, 9, 4)})
    sources = [ROOT / "course_knowledge_base/啟蒙交易班/整理成果/啟蒙交易班_課程整理.md",
               ROOT / "course_knowledge_base/啟蒙交易班/整理成果/四象限戰法強化_完整課程整理.md",
               ROOT / "course_knowledge_base/道氏三兄弟/整理成果/道氏三兄弟_一頁複習速查表.md",
               ROOT / "course_knowledge_base/戰法C班/整理成果/戰法C班_完整課程整理.md"]
    manifest = {"version": "ai-manual-replay-v1", "cohort_date": str(selected), "as_of": "2026-09-04",
                "created_utc": datetime.now(timezone.utc).isoformat(), "cohort": cohort,
                "calendar": calendar, "monitor_decision_bars": 20, "initial_cash": 500000,
                "budget": 10000, "analysis_author": "current conversation assistant; no model API invoked",
                "blindness": "NOT_BLIND: prior conversation knows some subsequent outcomes; aliases only reduce priming",
                "scope": "single full 19-stock radar cohort; no later additions, no reentries",
                "course_sources": [{"path": str(p), "sha256": digest(p)} for p in sources],
                "selection_artifacts": coverage,
                "engine_sha256": digest(__file__), "protocol_sha256": digest(RUN / "protocol.md")}
    save(RUN / "manifest.json", manifest, immutable=True)
    state = {"index": 0, "cash": 500000.0, "trades": {}, "plans": {}, "events": [],
             "equity": [], "sealed_decisions": []}
    save(RUN / "state.json", state)
    print(json.dumps({"candidates": len(cohort), "calendar": calendar, "decision_end": calendar[19],
                      "last_entry_date": calendar[20]}, ensure_ascii=False))


def packet():
    m, state = read(RUN / "manifest.json"), read(RUN / "state.json")
    if state["index"] >= len(m["calendar"]):
        print("COMPLETE"); return
    day = m["calendar"][state["index"]]
    frames = load_frames(m)
    items = []
    for item in m["cohort"]:
        alias = item["alias"]
        t = state["trades"].get(alias)
        if (t and t["remaining"] == 0) or (not t and state["index"] >= 20):
            continue
        f = frames[alias]
        history = f[f.date.dt.strftime("%Y-%m-%d") <= day].tail(140)
        bars = []
        for r in history.itertuples(index=False):
            bars.append({"date": r.date.date().isoformat(), "open": round(float(r.open), 4),
                         "high": round(float(r.high), 4), "low": round(float(r.low), 4),
                         "close": round(float(r.close), 4), "volume": int(r.volume),
                         "MA21": round(float(r.MA21), 4), "MA55": round(float(r.MA55), 4),
                         "ATR14": round(float(r.ATR14), 4)})
        previous_file = RUN / "sealed" / f"{m['calendar'][state['index']-1]}.json" if state["index"] else None
        prev = read(previous_file)["judgments"].get(alias) if previous_file and previous_file.exists() else None
        items.append({"alias": alias, "bars": bars, "position": t, "previous_judgment": prev})
    payload = {"as_of": day, "index": state["index"], "items": items,
               "execution_events": [x for x in state["events"] if x["date"] == day],
               "instructions": "AI must review every item. WAIT/ARM if flat; HOLD if held. AI defines meaningful structure, not numerical gate labels. Never read later bars."}
    path = RUN / "packets" / f"{day}.json"
    save(path, payload, immutable=True)
    print(f"DATE {day} PACKET_SHA256 {digest(path)}")
    print("id O H L C MA21 MA55 ATR VOL(last/20mean) position previous")
    for p in items:
        b = p["bars"][-1]; avg = sum(x["volume"] for x in p["bars"][-20:]) / 20
        t = p["position"]
        position = "FLAT" if not t else f"q={t['remaining']} entry={t['entry_price']:.2f} phase={t['phase']} defense={t['dynamic_defense']:.2f} pending={t['pending']}"
        prev = "" if not p["previous_judgment"] else p["previous_judgment"]["reason"]
        print(p["alias"], *[b[k] for k in ("open", "high", "low", "close", "MA21", "MA55", "ATR14")],
              round(b["volume"] / avg, 2) if avg else 0, position, prev)
    for e in payload["execution_events"]: print("EXEC", json.dumps(e, ensure_ascii=False))


def validate_judgments(payload, packet_data, frames, state):
    if payload["date"] != packet_data["as_of"]:
        raise ValueError("wrong decision date")
    path = RUN / "packets" / f"{payload['date']}.json"
    if payload["packet_sha256"] != digest(path):
        raise ValueError("decision does not cite exact packet")
    if set(payload["judgments"]) != {p["alias"] for p in packet_data["items"]}:
        raise ValueError("all candidates must receive an explicit AI judgment")
    for p in packet_data["items"]:
        alias = p["alias"]; j = payload["judgments"][alias]; b = p["bars"][-1]
        if len(j.get("reason", "")) < 8: raise ValueError("missing AI rationale")
        held = p["position"] is not None
        if j["action"] not in ({"HOLD"} if held else {"WAIT", "ARM"}):
            raise ValueError("invalid action for position")
        if j["action"] == "ARM":
            trigger, defense, cap = [float(j[k]) for k in ("trigger", "defense", "cap")]
            if not (0 < defense < b["close"] < trigger <= cap):
                raise ValueError(f"invalid entry plan {alias}")
            if (cap - defense) / cap > .10 or (cap - defense) > 2.5 * b["ATR14"]:
                raise ValueError(f"risk cap exceeded {alias}")
        if "defense_date" in j:
            dates = {x["date"]: x for x in p["bars"]}
            if j["defense_date"] not in dates:
                raise ValueError("defense uses invisible data")
            value = j.get("defense", j.get("raise_defense"))
            if value is not None and abs(value - dates[j["defense_date"]]["low"]) > .011:
                raise ValueError(f"defense not linked to quoted low {alias}")
        if "raise_defense" in j:
            if not held or j["raise_defense"] >= b["close"]:
                raise ValueError("cannot retroactively raise stop above current close")


def apply_close(state, payload, frames):
    day = payload["date"]
    for alias, j in payload["judgments"].items():
        row = row_for(frames[alias], day)
        if row is None: continue
        t = state["trades"].get(alias)
        if not t:
            if j["action"] == "ARM":
                state["plans"][alias] = {**j, "signal_date": day}
            continue
        if t["remaining"] == 0: continue
        close = float(row.close)
        t["mark"] = close
        t["mfe_price"] = max(t["mfe_price"], float(row.high))
        if not t["runner"] and row.high >= t["entry_price"] + 2*(t["entry_price"]-t["initial_defense"]):
            t["runner"] = True; t["phase"] = "TREND_RUNNER（波段延伸期）"
            t["activation_date"] = day
            t["dynamic_defense"] = max(t["entry_price"], t["dynamic_defense"])
            t["below_streak"] = 0
        t["below_streak"] = t["below_streak"] + 1 if close < row.MA21 else 0
        reason = None
        if close < t["initial_defense"]: reason = "跌破初始防線"
        elif t["runner"] and close < t["dynamic_defense"]: reason = "跌破成本或AI確認樞紐"
        elif t["runner"] and t["below_streak"] >= 2: reason = "波段期連兩日跌破21MA"
        elif not t["runner"] and close < row.MA21 and close < t["dynamic_defense"]:
            reason = "21MA及AI確認結構同步失守"
        if reason:
            t["pending"] = "SELL"; t["exit_reason"] = reason; t["exit_signal_date"] = day
        elif not t["runner"] and close < row.MA21 and not t["half_used"]:
            t["pending"] = "HALF"; t["half_used"] = True
            t["phase"] = "EARLY_WEAKNESS（早期轉弱）"
        elif not t["runner"] and close >= row.MA21:
            t["phase"] = "INITIAL_RISK（初始風險期）"
        if "raise_defense" in j and not t["pending"]:
            t["dynamic_defense"] = max(t["dynamic_defense"], float(j["raise_defense"]))
        if t["pending"]:
            state["events"].append({"date": day, "alias": alias, "event": "CLOSE_SIGNAL",
                                    "order": t["pending"], "reason": reason or "初期首次21MA轉弱減半"})
    market_value = sum(sell(t["remaining"], t["mark"]) for t in state["trades"].values())
    state["equity"].append({"date": day, "cash": state["cash"], "net_market_value": market_value,
                            "equity": state["cash"] + market_value})


def execute_next(state, frames, day):
    for alias, t in state["trades"].items():
        row = row_for(frames[alias], day)
        if not t["pending"] or row is None or row.volume <= 0: continue
        q = t["remaining"] if t["pending"] == "SELL" else t["quantity"] // 2
        p = float(row.open); cash = sell(q, p)
        t["remaining"] -= q; t["sale_proceeds"] += cash; state["cash"] += cash
        event = {"date": day, "alias": alias, "event": t["pending"], "shares": q, "price": p, "cash": cash}
        t["fills"].append(event); state["events"].append(event)
        if not t["remaining"]:
            t["exit_date"] = day; t["exit_price"] = p; t["phase"] = "CLOSED（交易已結束）"
        t["pending"] = None
    for alias, plan in state["plans"].items():
        row = row_for(frames[alias], day)
        if row is None or row.volume <= 0: continue
        if row.open <= plan["defense"] or row.open > plan["cap"] or row.high < plan["trigger"]:
            continue
        price = max(float(row.open), plan["trigger"])
        q = math.floor(10000 / (price * 1.001425)); out = buy(q, price)
        if state["cash"] < out: raise ValueError("insufficient cash; cannot silently skip")
        state["cash"] -= out
        event = {"date": day, "alias": alias, "event": "BUY", "shares": q, "price": price, "cash": -out}
        state["trades"][alias] = {"entry_date": day, "entry_price": price, "quantity": q, "remaining": q,
            "buy_outflow": out, "sale_proceeds": 0., "initial_defense": plan["defense"],
            "dynamic_defense": plan["defense"], "runner": False, "phase": "INITIAL_RISK（初始風險期）",
            "below_streak": 0, "half_used": False, "pending": None, "mark": price,
            "mfe_price": price, "fills": [event], "plan": plan}
        state["events"].append(event)
    state["plans"] = {}


def advance():
    m, state = read(RUN / "manifest.json"), read(RUN / "state.json")
    day = m["calendar"][state["index"]]
    path = RUN / "decisions" / f"{day}.json"
    payload = read(path); p = read(RUN / "packets" / f"{day}.json")
    frames = load_frames(m)
    validate_judgments(payload, p, frames, state)
    seal = RUN / "sealed" / f"{day}.json"
    save(seal, payload, immutable=True)
    state["sealed_decisions"].append({"date": day, "sha256": digest(seal),
                                     "sealed_utc": datetime.now(timezone.utc).isoformat()})
    apply_close(state, payload, frames)
    save(RUN / "checkpoints" / f"{day}.json", state, immutable=True)
    state["index"] += 1
    if state["index"] < len(m["calendar"]):
        execute_next(state, frames, m["calendar"][state["index"]])
    save(RUN / "state.json", state)
    packet()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "packet", "advance"])
    args = parser.parse_args()
    {"init": init_run, "packet": packet, "advance": advance}[args.command]()


if __name__ == "__main__": main()

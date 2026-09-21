"""Audit an already sealed AI replay and account for its outcomes; no new verdicts."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import course_ai_manual_replay as engine


def initial_state(cash=500000):
    return {"index": 0, "cash": float(cash), "trades": {}, "plans": {},
            "events": [], "equity": [], "sealed_decisions": []}


def audit_run(run=engine.RUN):
    manifest, final = engine.read(run / "manifest.json"), engine.read(run / "state.json")
    assert final["index"] == len(manifest["calendar"]), "unfinished replay"
    assert engine.digest(engine.__file__) == manifest["engine_sha256"], "execution engine changed"
    assert engine.digest(run / "protocol.md") == manifest["protocol_sha256"], "protocol changed"
    for source in manifest["course_sources"]:
        assert engine.digest(source["path"]) == source["sha256"], "course source changed"
    frames = engine.load_frames(manifest)  # Also validates all price source hashes.
    state = initial_state(manifest["initial_cash"])
    decisions = []
    for index, day in enumerate(manifest["calendar"]):
        packet_path = run / "packets" / f"{day}.json"
        packet = engine.read(packet_path)
        payload = engine.read(run / "sealed" / f"{day}.json")
        assert engine.read(run / "decisions" / f"{day}.json") == payload
        assert engine.digest(run / "sealed" / f"{day}.json") == final["sealed_decisions"][index]["sha256"]
        assert packet["as_of"] == day and packet["index"] == index
        expected = {c["alias"] for c in manifest["cohort"]
                    if (c["alias"] not in state["trades"] and index < manifest["monitor_decision_bars"])
                    or state["trades"].get(c["alias"], {}).get("remaining", 0) > 0}
        assert {p["alias"] for p in packet["items"]} == expected
        for item in packet["items"]:
            assert item["position"] == state["trades"].get(item["alias"])
            assert item["bars"][-1]["date"] == day
            assert all(b["date"] <= day for b in item["bars"]), "future input"
            f = frames[item["alias"]]
            history = f[f.date.dt.strftime("%Y-%m-%d") <= day].tail(140)
            assert list(history.date.dt.strftime("%Y-%m-%d")) == [b["date"] for b in item["bars"]]
            for bar, row in zip(item["bars"], history.itertuples(index=False)):
                for key in ("open", "high", "low", "close", "MA21", "MA55", "ATR14"):
                    assert bar[key] == round(float(getattr(row, key)), 4)
                assert bar["volume"] == int(row.volume)
            previous = decisions[-1]["judgments"].get(item["alias"]) if decisions else None
            assert item["previous_judgment"] == previous
        engine.validate_judgments(payload, packet, frames, state)
        state["sealed_decisions"].append(deepcopy(final["sealed_decisions"][index]))
        engine.apply_close(state, payload, frames)
        assert state == engine.read(run / "checkpoints" / f"{day}.json"), f"checkpoint mismatch {day}"
        state["index"] += 1
        if state["index"] < len(manifest["calendar"]):
            engine.execute_next(state, frames, manifest["calendar"][state["index"]])
        decisions.append(payload)
    assert state == final, "final replay mismatch"
    return manifest, final, decisions


def accounting(manifest, state):
    mapping = {c["alias"]: c for c in manifest["cohort"]}
    trades = []
    for alias, t in state["trades"].items():
        net_mark = engine.sell(t["remaining"], t["mark"])
        remaining_cost = t["buy_outflow"] * t["remaining"] / t["quantity"]
        realized = t["sale_proceeds"] - (t["buy_outflow"] - remaining_cost)
        unrealized = net_mark - remaining_cost
        pnl = realized + unrealized
        gross_return = ((sum(f["shares"] * f["price"] for f in t["fills"][1:])
                        + t["remaining"] * t["mark"]) / (t["quantity"] * t["entry_price"]) - 1) * 100
        last_date = t.get("exit_date", manifest["as_of"])
        trades.append({"alias": alias, "code": mapping[alias]["code"], "name": mapping[alias]["name"],
            "entry_date": t["entry_date"], "entry_price": t["entry_price"],
            "quantity": t["quantity"], "remaining": t["remaining"],
            "status": "OPEN（持有中）" if t["remaining"] else "CLOSED（交易已結束）",
            "phase": t["phase"], "initial_defense": t["initial_defense"],
            "dynamic_defense": t["dynamic_defense"], "last_date": last_date,
            "exit_price_or_mark": t.get("exit_price", t["mark"]),
            "buy_outflow": t["buy_outflow"], "sale_proceeds": t["sale_proceeds"],
            "net_market_value": net_mark, "realized_pnl": realized,
            "unrealized_net_pnl": unrealized, "net_pnl": pnl,
            "net_return_pct": pnl / t["buy_outflow"] * 100,
            "gross_weighted_return_pct": gross_return,
            "mfe_pct": (t["mfe_price"] / t["entry_price"] - 1) * 100,
            "mfe_price": t["mfe_price"], "fills": t["fills"],
            "entry_rationale": t["plan"]["reason"],
            "exit_reason": t.get("exit_reason"), "exit_signal_date": t.get("exit_signal_date")})
    realized = sum(t["realized_pnl"] for t in trades)
    unrealized = sum(t["unrealized_net_pnl"] for t in trades)
    pnl = realized + unrealized
    equity = state["cash"] + sum(t["net_market_value"] for t in trades)
    assert math.isclose(equity - manifest["initial_cash"], pnl, abs_tol=1e-7)
    assert math.isclose(equity, state["equity"][-1]["equity"], abs_tol=1e-7)
    cash = float(manifest["initial_cash"])
    cash_min, max_positions, holdings = cash, 0, {}
    for event in state["events"]:
        if "cash" not in event: continue
        cash += event["cash"]
        cash_min = min(cash_min, cash)
        delta = event["shares"] if event["event"] == "BUY" else -event["shares"]
        holdings[event["alias"]] = holdings.get(event["alias"], 0) + delta
        assert holdings[event["alias"]] >= 0
        max_positions = max(max_positions, sum(q > 0 for q in holdings.values()))
    assert math.isclose(cash, state["cash"], abs_tol=1e-7)
    peak, max_dd_cash, max_dd_pct = manifest["initial_cash"], 0., 0.
    for day in state["equity"]:
        peak = max(peak, day["equity"])
        max_dd_cash = max(max_dd_cash, peak - day["equity"])
        max_dd_pct = max(max_dd_pct, (peak-day["equity"]) / peak * 100)
    profits = sum(max(t["net_pnl"], 0) for t in trades)
    losses = -sum(min(t["net_pnl"], 0) for t in trades)
    closed_pnl = sum(t["net_pnl"] for t in trades if not t["remaining"])
    stats = {"trades": len(trades), "closed": sum(t["remaining"] == 0 for t in trades),
        "open": sum(t["remaining"] > 0 for t in trades),
        "winners_including_marked_open": sum(t["net_pnl"] > 0 for t in trades),
        "closed_trade_net_pnl": closed_pnl,
        "open_partial_realized_pnl": realized - closed_pnl,
        "all_realized_pnl": realized, "open_unrealized_net_pnl": unrealized,
        "total_net_pnl": pnl, "ending_equity": equity, "ending_cash": state["cash"],
        "account_return_pct": pnl / manifest["initial_cash"] * 100,
        "sum_entry_outflows": sum(t["buy_outflow"] for t in trades),
        "on_sum_entries_return_pct": pnl / sum(t["buy_outflow"] for t in trades) * 100,
        "trade_mean_net_return_pct": sum(t["net_return_pct"] for t in trades) / len(trades),
        "profit_factor_including_marked_open": profits / losses if losses else None,
        "max_concurrent_positions": max_positions, "peak_cash_deployed": manifest["initial_cash"] - cash_min,
        "max_drawdown_cash": max_dd_cash, "max_drawdown_account_pct": max_dd_pct}
    return trades, stats


def cost_sensitivity(state, min_fee=0., slippage=0.):
    """Same sealed reference orders/dates; accounting stress, not a new signal replay."""
    def fee(notional): return max(min_fee, notional * .001425) if notional > 0 else 0.
    def proceeds(q, p):
        amount = q * p * (1-slippage)
        return amount - fee(amount) - amount * .003
    rows = []
    for alias, t in state["trades"].items():
        price = t["entry_price"] * (1+slippage)
        quantity = math.floor(10000 / price)
        while quantity * price + fee(quantity * price) > 10000:
            quantity -= 1
        outflow = quantity * price + fee(quantity * price)
        remaining, returned = quantity, 0.
        for fill in t["fills"][1:]:
            q = quantity // 2 if fill["event"] == "HALF" else remaining
            returned += proceeds(q, fill["price"])
            remaining -= q
        rows.append({"alias": alias, "quantity": quantity,
                     "net_pnl": returned + proceeds(remaining, t["mark"]) - outflow})
    return {"minimum_commission": min_fee, "one_way_slippage": slippage,
            "net_pnl": sum(r["net_pnl"] for r in rows), "trades": rows}


def make_report():
    run = engine.RUN
    manifest, state, decisions = audit_run(run)
    trades, stats = accounting(manifest, state)
    counts = Counter(j["action"] for d in decisions for j in d["judgments"].values())
    cohort = []
    for c in manifest["cohort"]:
        plans = [{"date": d["date"], **d["judgments"][c["alias"]]} for d in decisions
                 if d["judgments"].get(c["alias"], {}).get("action") == "ARM"]
        last = next(d["judgments"][c["alias"]] for d in reversed(decisions) if c["alias"] in d["judgments"])
        cohort.append({"alias": c["alias"], "code": c["code"], "name": c["name"],
            "plans": plans, "last_judgment": last,
            "disposition": "ENTERED（曾成交）" if c["alias"] in state["trades"] else
                           "EXPIRED_UNFILLED（計畫未成交、監控到期）" if plans else
                           "EXPIRED_NO_SETUP（未建立進場計畫、監控到期）"})
    sensitivity = [cost_sensitivity(state), cost_sensitivity(state, 20), cost_sensitivity(state, 20, .001)]
    assert math.isclose(sensitivity[0]["net_pnl"], stats["total_net_pnl"], abs_tol=1e-7)
    audit = {"sealed_days": len(decisions), "candidate_judgments": sum(counts.values()),
        "judgment_actions": dict(counts), "ai_structure_raises": sum("raise_defense" in j for d in decisions for j in d["judgments"].values()),
        "exact_checkpoint_replay": True, "ledger_reconciled": True,
        "packet_values_and_date_truncation_verified": True,
        "engine_protocol_course_and_price_hashes_unchanged": True,
        "strict_blind_test": False, "real_market_execution_validated": False}
    quality_review = engine.read(run / "data_quality_review.json")
    audit["corporate_actions_accounted"] = False
    result = {"scope": manifest["scope"], "as_of": manifest["as_of"], "audit": audit,
              "validity": quality_review,
              "summary": stats, "trades": trades, "cohort": cohort,
              "cost_sensitivity_fixed_reference_orders": sensitivity,
              "daily_net_equity": state["equity"]}
    engine.save(run / "result.json", result)
    money = lambda x: f"{x:+,.2f}"
    lines = ["# 19 檔 AI 歷史回放試跑結果", "",
        "**效力：INVALID_FOR_PROFITABILITY（不得用於獲利能力判定）。** AI 判讀流程已跑完，但回測處理漏掉除權息調整及股利權益，且已查到具體受影響交易。下列數字保留為未修正的原始流程輸出，不是有效含息績效。",
        "",
        "這次由本對話 AI 逐日閱讀截斷的量價資料與定期 K 線圖，親自決定是否建立進場計畫及如何確認結構防線；不是把既有程式篩選結果改名為 AI。程式負責固定風控、成交模擬與記帳。",
        "", "## 結論與適用範圍", "",
        f"2026/07/17 雷達完整 19 檔，20 個監控決策日（至 8/13，最後計畫可在 8/14 成交），已成交部位逐日追蹤至 9/4。實際進場 {stats['trades']} 筆，已結束 {stats['closed']} 筆、持有 {stats['open']} 筆。",
        f"按現有快取價格、每筆 1 萬元含買費及整數零股計算，原始流程輸出的含期末持倉淨估值總損益為 **{money(stats['total_net_pnl'])} 元**。此數字未處理完整公司行動，不應解讀為 AI 策略虧損的有效證據。",
        "本輪不能證明 AI 能穩定獲利，也不能與前次不同名單的 90 筆機械回測直接比較。",
        "", "## 查核後確認的回測缺陷", "",
        "裕融公司公告載明 2026/9/1 除權息，每股現金股利4.38元、股票股利0.2元，9/23發放。資料來源：[公司重大訊息（玉山證券轉載）](https://m.esunsec.com.tw/news/instant-detail.aspx?id=%7BCD6A64E9-C0CA-4DB6-A660-4C5F4DA51F17%7D)。",
        "快取8/31 close為85.196075、adj_close為80.816078；9/1兩者均為80。本輪沒有使用一致的還原口徑，直接把9/1的80與先前81.7647防線相比，判為全出；也沒有股利及配股權益帳。這是本輪回測方法的缺陷，不能歸咎於選股策略失敗。",
        "只把股利加回原交易並不足夠：修正圖形、均線、防線後，當天是否應出場也可能不同。原封存判讀不修改，正式重跑需要先重建截至當日一致的分析價格、實際交易價格與公司行動帳。修正後可能較好或較差，目前沒有有效結果。",
        "", "## 未修正帳本的損益口徑", "", "|項目|金額／數值|", "|---|---:|",
        f"|6 筆已結束交易損益|{money(stats['closed_trade_net_pnl'])} 元|",
        f"|仍持有交易已減碼部分損益|{money(stats['open_partial_realized_pnl'])} 元|",
        f"|全部已實現損益|{money(stats['all_realized_pnl'])} 元|",
        f"|剩餘部位未實現淨損益（含預估賣費稅）|{money(stats['open_unrealized_net_pnl'])} 元|",
        f"|合計|{money(stats['total_net_pnl'])} 元|",
        f"|8 筆實際買進支出合計|{stats['sum_entry_outflows']:,.2f} 元|",
        f"|總損益／買進支出合計|{stats['on_sum_entries_return_pct']:+.2f}%|",
        f"|50 萬帳戶期末淨權益|{stats['ending_equity']:,.2f} 元|",
        f"|50 萬帳戶報酬|{stats['account_return_pct']:+.3f}%|",
        f"|最多同時持有|{stats['max_concurrent_positions']} 檔|",
        f"|模擬現金占用峰值|{stats['peak_cash_deployed']:,.2f} 元|",
        f"|50 萬帳戶最大收盤淨值回落|{stats['max_drawdown_account_pct']:.3f}%（{stats['max_drawdown_cash']:,.2f} 元）|",
        f"|獲利交易數（含期末持倉估值）|{stats['winners_including_marked_open']}/{stats['trades']}|",
        f"|含期末估值 Profit factor（獲利因子）|{stats['profit_factor_including_marked_open']:.3f}|",
        "", "已實現以賣出股數分攤原買進支出；未實現以剩餘股數成本對照期末扣費稅市值。買進支出合計不是最高同時需準備資金；現金占用峰值假設交易款立即可用，未模擬券商交割可用額度。閒置現金占比高，帳戶回撤不能代表滿倉風險。",
        "", "## 逐筆成交與表現", "",
        "MFE（持有期間最高價相對進場價的最大有利幅度）是未扣費的價格漲幅，不是實際已賺到，也沒有假設全數能賣在最高點；總報酬已按減碼、剩餘部位及費稅加權。出場當日開盤後的高點不計入 MFE。",
        "", "|股票|進場日／價|原股數→餘股|狀態／最後出場或估值日|最後價|MFE|扣成本加權報酬|總淨損益|",
        "|---|---|---:|---|---:|---:|---:|---:|"]
    for t in trades:
        lines.append(f"|{t['code']} {t['name']}|{t['entry_date']}／{t['entry_price']:.4f}|{t['quantity']}→{t['remaining']}|{t['status']} {t['last_date']}|{t['exit_price_or_mark']:.4f}|{t['mfe_pct']:+.2f}%|{t['net_return_pct']:+.2f}%|{money(t['net_pnl'])}|")
    lines += ["", "### 各筆成交明細與 AI 理由", ""]
    for t in trades:
        lines += [f"#### {t['code']} {t['name']}", "", f"進場判讀：{t['entry_rationale']}",
            f"初始防線 {t['initial_defense']:.4f}；最終保存防線 {t['dynamic_defense']:.4f}。",
            f"出場原因：{t['exit_reason'] or '未觸發全出，剩餘部位期末估值'}。",
            "", "|日期|動作|股數|參考成交價|現金流（扣費稅）|", "|---|---|---:|---:|---:|"]
        for f in t["fills"]:
            label = {"BUY": "BUY（買進）", "HALF": "HALF（減原部位一半）", "SELL": "SELL（全數出場）"}[f["event"]]
            lines.append(f"|{f['date']}|{label}|{f['shares']}|{f['price']:.4f}|{money(f['cash'])}|")
        lines += [f"", f"已實現 {money(t['realized_pnl'])} 元；未實現淨損益 {money(t['unrealized_net_pnl'])} 元。", ""]
    lines += ["## 全部 19 檔去向", "", "|股票|AI 建立計畫日數（含續訂）|結果|", "|---|---:|---|"]
    for c in cohort:
        lines.append(f"|{c['code']} {c['name']}|{len(c['plans'])}|{c['disposition']}|")
    lines += ["", "未成交不等於每天都沒有機會：有些股票曾建立計畫，但隔天未觸發，或之後被 AI 撤回。仁寶曾有計畫，8/10 撤回後 8/12 上漲；本輪照實列為錯過，沒有事後補填買進。所有未成交理由及每日計畫都保留在封存 JSON。",
        "", "## 成本壓力情境", "",
        "以下固定原決策、原參考成交日期及出場訊號，僅重算整數股數、成交成本與估值；滑價不回頭重算 +2R、防線或追價上限。它是記帳壓力測試，不是另一次完整策略回測。",
        "", "|情境|總淨損益|", "|---|---:|"]
    for label, s in zip(["基礎：買賣各 0.1425%、賣稅 0.3%", "每次委託最低手續費 20 元", "最低 20 元＋買賣各 0.1% 不利滑價（含期末預估賣出）"], sensitivity):
        lines.append(f"|{label}|{money(s['net_pnl'])} 元|")
    lines += ["", "手續費與最低收費均為研究假設，不代表你的券商報價；實際費率由券商訂定。普通股票賣出稅 0.3% 的依據見[證交所投資指南](https://www.twse.com.tw/zh/about/company/guide.html)。本輪未四捨五入每笔費稅至整元。",
        "", "## 封存與驗證", "",
        f"共 {audit['sealed_days']} 個決策日、{audit['candidate_judgments']} 筆逐股判讀：WAIT（等待）{counts['WAIT']}、ARM（建立進場計畫）{counts['ARM']}、HOLD（持倉檢視）{counts['HOLD']}；AI 明確上移結構防線 {audit['ai_structure_raises']} 次。ARM 次數包括未成交及隔日續訂，不等於交易筆數。",
        "HOLD（持倉檢視）表示該日不新增進場計畫，仍會執行固定出場規則；因此當天 HOLD 判讀可以同時指出 EXIT_TRIGGERED（已觸發出場）。",
        "逐一驗證 36 份輸入只到當日、全候選覆蓋、指標與快取一致、原始決策與 SHA256 封存一致；從零重播所有封存決策，逐日檢查點與最終帳本完全一致。原執行程式、規格、課程及價格檔雜湊未變。雜湊證明檔案一致，不是外部時間戳認證，也不能證明模型沒有先驗記憶。",
        "", "## 不能忽略的限制", "",
        "1. 非嚴格盲測：本對話已知部分後續行情。按日截斷及別名只是降低偏差，不是獨立樣本外證據。",
        "2. 這是 AI 進場／結構判讀＋固定狀態切換出場，不是 AI 自由裁量整套買賣；也不是 Minimax M3 的測試。太極整合在啟蒙判讀中，並非每筆都被認定為太極成功型態。",
        "3. 使用現存快取，不是歷史當日封存版本。部分 OHLC 有非標準小數，第一金8/11、兆豐金8/13附近價差尚待逐事件查核；裕融9/1除權息已查證，不能將該次股價落差全視為市場下跌。不能將一檔的發現直接套到全部股票。",
        "4. 現金股利、配股、減資及跨資料來源調整口徑未重建；若訊號用未調整價格又未計股利，可能把除息誤判為結構失守；若使用事後還原價格，價格與股數也未必可交易。這會同時影響 AI 判讀、出場和損益，不能只加回一筆股利就視為修正完成。證交所提供[除權除息參考價說明與試算](https://investoredu.twse.com.tw/Pages/TWSE_ServiceArea3_1.aspx)。",
        "5. 日 K 無法保證盤中触價真的成交；未模擬漲跌停排隊、零股流動性、價格跳動單位及撮合順序，入場當日盤中先後順序未知。",
        "6. 19 檔中 8 筆成交，且金融股集中、同一批名單與時期，不能推估穩定勝率或長期正期望。",
        "7. 德昌已減半、期末低於21MA，但未達2R且未破65.5；依凍結策略仍持有。其主狀態 INITIAL_RISK（初始風險期）不代表走勢健康，另有 EARLY_WEAKNESS（早期轉弱）事實。此處未為改善結果改出場規則。",
        "", "## 可重現資料", "",
        f"- [事前規格]({(run / 'protocol.md').as_posix()})",
        f"- [完整結果 JSON]({(run / 'result.json').as_posix()})",
        f"- [公司行動缺陷查核紀錄]({(run / 'data_quality_review.json').as_posix()})",
        f"- [測試紀錄：41 項通過；不代表績效有效]({(run / 'verification.json').as_posix()})",
        f"- [最終逐日帳本]({(run / 'state.json').as_posix()})",
        f"- [首日 AI 判讀]({(run / 'sealed/2026-07-17.json').as_posix()})",
        f"- [最終日 AI 判讀]({(run / 'sealed/2026-09-04.json').as_posix()})",
        "", "重算與稽核：`python -X utf8 scripts/course_ai_replay_report.py`。這只重播已封存的 AI 決定，不會自動生成新的 AI 判讀。", ""]
    (run / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(__import__("json").dumps({"audit": audit, "summary": stats, "sensitivity": [{k:v for k,v in x.items() if k != 'trades'} for x in sensitivity]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    make_report()

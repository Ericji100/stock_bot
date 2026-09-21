"""Equal-notional accounting on fixed-defense signals; no signal optimization.

Original shares and bonus rights are separate tranches. Unconfirmed stock
distribution dates stay valuation-only, never manufactured cash proceeds.
Fractional share units are theoretical, matching the original percentage test.
"""
from __future__ import annotations

from collections import Counter
from functools import lru_cache
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.course_corporate_action_data import RUN, SOURCE, AS_OF, read, save, digest
from scripts.course_corporate_action_backtest import load_inputs

BUDGET = 10_000.
BUY_FEE = SELL_FEE = .001425
SELL_TAX = .003
MIN_FEE = 20.


def commission(amount):
    return max(MIN_FEE, amount*BUY_FEE) if amount > 0 else 0.


def account_trade(trade, raw, terms=None, budget=BUDGET):
    """Marked total wealth, including non-reinvested receivables, not just cash."""
    terms = terms or {}
    qty = budget/trade['entry_price']
    lots = [{'units': 1., 'available': trade['entry_date'], 'kind': 'ORIGINAL', 'source': None}]
    for event in trade['action_events']:
        ratio = event['ratio']
        if ratio > 1:
            info = terms.get(event['date'], {})
            lots.append({'units': event['units_before']*(ratio-1),
                         'available': info.get('stock_release_date'), 'kind': 'BONUS',
                         'source': info.get('source'), 'ex_date': event['date'],
                         'certificate_release_date': info.get('certificate_release_date')})
        elif ratio < 1:
            raise ValueError('A held capital reduction requires explicit settlement terms')
    assert math.isclose(sum(l['units'] for l in lots),trade['units'],abs_tol=1e-8)
    end = raw.iloc[-1]
    sales, unsold = [], []
    for lot in lots:
        sale_day = max(trade['exit_date'],lot['available']) if trade['exit_date'] and lot['available'] else None
        sell_rows = raw[raw.date.dt.strftime('%Y-%m-%d') >= sale_day] if sale_day else raw.iloc[0:0]
        if len(sell_rows):
            row = sell_rows.iloc[0]
            sales.append({**lot,'date':row.date.date().isoformat(),'price':float(row.open),
                          'quantity':qty*lot['units'],'amount':qty*lot['units']*float(row.open)})
        else:
            unsold.append({**lot,'quantity':qty*lot['units'],'mark_date':end.date.date().isoformat(),
                           'mark_price':float(end.close),'value':qty*lot['units']*float(end.close)})
    # An already-exited original position can retain bonus rights. Flag any
    # later ex-date that needs entitlement-specific processing, rather than miss it.
    dividends=[]
    for c in trade['cash_entitlements']:
        payment=terms.get(c['ex_date'],{}).get('cash_pay_date',c['pay_date'])
        dividends.append({'ex_date':c['ex_date'],'pay_date':payment,'amount':qty*c['amount'],
                          'state':'PAID' if payment and payment<=AS_OF else
                                  'RECEIVABLE' if payment else 'PAY_DATE_UNVERIFIED'})
    paid = sum(d['amount'] for d in dividends if d['state']=='PAID')
    due = sum(d['amount'] for d in dividends if d['state']=='RECEIVABLE')
    unknown = sum(d['amount'] for d in dividends if d['state']=='PAY_DATE_UNVERIFIED')
    stock_value = sum(l['value'] for l in unsold)
    sale_value = sum(s['amount'] for s in sales)
    price_pnl = sale_value + stock_value - budget
    pnl = price_pnl + paid + due + unknown
    # Aggregate same-day same-symbol tranches into one sale for minimum fees.
    sale_by_day = {}
    for sale in sales: sale_by_day[sale['date']] = sale_by_day.get(sale['date'],0)+sale['amount']
    fees = commission(budget) + sum(commission(a)+a*SELL_TAX for a in sale_by_day.values())
    liquidation = commission(stock_value)+stock_value*SELL_TAX if stock_value else 0.
    status = 'OPEN（持有中）' if not trade['exit_date'] else 'CLOSED（交易已結束）'
    if trade['exit_date'] and unsold: status='EXITED_WITH_RIGHTS（主部位已出場、配股權益待處理）'
    if trade['status']=='EXIT_TRIGGERED':status='EXIT_TRIGGERED（已觸發出場、待下個交易日）'
    # Independent cash/assets identity. Receivables are assets, not available cash.
    assets = sale_value + stock_value + paid + due + unknown
    assert math.isclose(assets-budget,pnl,abs_tol=1e-7)
    return {'trade_id':trade['trade_id'],'code':trade['code'],'name':trade['name'],
            'entry_date':trade['entry_date'],'entry_price':trade['entry_price'],'quantity':qty,
            'initial_defense':trade['initial_defense'],'last_equivalent_defense':trade['defense'],
            'exit_trigger_date':trade['exit_trigger_date'],'core_exit_date':trade['exit_date'],
            'core_exit_price':trade['exit_price'],'status':status,'notional':budget,
            'dividends':dividends,'dividend_paid':paid,'dividend_receivable':due,
            'dividend_payment_unverified':unknown,'dividend_total':paid+due+unknown,
            'sales':sales,'unsold_tranches':unsold,'sales_value':sale_value,'stock_and_rights_value':stock_value,
            'price_pnl':price_pnl,'gross_pnl':pnl,'gross_return_pct':pnl/budget*100,
            'fees_and_transaction_tax':fees,'net_pnl':pnl-fees,'net_return_pct':(pnl-fees)/budget*100,
            'estimated_final_liquidation_cost':liquidation,
            'mfe_pct':trade['mfe_pct'],'mfe_date':trade['mfe_date'],
            'same_day_liquid_bonus_reference_pct':trade['performance_return_pct'],
            'stock_entitlement_settlement_impact':pnl-budget*trade['performance_return_pct']/100,
            'action_events':trade['action_events']}


def summarize(rows):
    total = lambda k: sum(r[k] for r in rows)
    n = len(rows)
    closed = [r for r in rows if r['core_exit_date']]
    positive=sum(r['gross_pnl']>0 for r in rows)
    return {'trades':n,'closed_core_positions':len(closed),'open_core_positions':n-len(closed),
            'statuses':dict(Counter(r['status'] for r in rows)),
            'cumulative_entry_notional':total('notional'),
            **{k:total(k) for k in ['gross_pnl','net_pnl','price_pnl','dividend_total','dividend_paid',
                'dividend_receivable','dividend_payment_unverified','fees_and_transaction_tax',
                'sales_value','stock_and_rights_value','estimated_final_liquidation_cost',
                'stock_entitlement_settlement_impact']},
            'gross_return_pct':total('gross_pnl')/total('notional')*100,
            'net_return_pct':total('net_pnl')/total('notional')*100,
            'mean_mfe_pct':total('mfe_pct')/n,'positive_trades':positive,'positive_rate_pct':positive/n*100,
            'closed_group_gross_pnl':sum(r['gross_pnl'] for r in closed),
            'open_group_gross_pnl':sum(r['gross_pnl'] for r in rows if not r['core_exit_date'])}


def build():
    bt, old = read(RUN/'backtest.json'), read(SOURCE)
    assert not bt['errors']
    assert all(old['parameters'][k]==v for k,v in bt['parameters'].items())
    assert digest(SOURCE)==read(RUN/'input_manifest.json')['source_sha256']
    items={i['code']:i for i in read(RUN/'input_manifest.json')['items']}
    terms=read(RUN/'settlement_terms.json')
    @lru_cache(None)
    def inputs(code):return load_inputs(items[code])
    def ledger(trades):
        rows=[]
        for t in trades:
            raw,events,_=inputs(t['code'])
            row=account_trade(t,raw,terms.get(t['code']))
            entry=raw.loc[raw.date.dt.strftime('%Y-%m-%d')==t['entry_date']].iloc[0]
            assert entry.low-.0001 <= t['entry_price'] <= entry.high+.0001
            assert all(t['entry_date']<d['ex_date'] for d in row['dividends'])
            assert all(s['date']>=s['available'] for s in row['sales'])
            assert not t['exit_date'] or t['exit_trigger_date']<t['exit_date']
            if t['exit_date'] and any(e['ratio']>1 for e in t['action_events']):
                later=[e for e in events if e['date']>t['exit_date'] and(e['cash'] or e['ratio']!=1)]
                assert not later, f"Post-exit bonus rights need additional actions: {t['code']} {later}"
            rows.append(row)
        return sorted(rows,key=lambda r:(r['entry_date'],r['code']))
    new=ledger([t for c in bt['candidates'] for t in c['trades']])
    fixed=ledger(bt['fixed_original_entries'])
    old_trades=[t for c in old['candidates'] for t in c['trades']]
    old_summary={'trades':len(old_trades),'gross_pnl':sum(t['performance_return_pct'] for t in old_trades)*BUDGET/100,
        'gross_return_pct':sum(t['performance_return_pct'] for t in old_trades)/len(old_trades),
        'closed_core_positions':sum(t['status']=='CLOSED' for t in old_trades),
        'open_core_positions':sum(t['status']!='CLOSED' for t in old_trades)}
    fixed_keys={(r['code'],r['entry_date'])for r in fixed}
    excluded=[{'code':t['code'],'name':t['name'],'entry_date':t['entry_date'],
        'reason':'Original entry is on a zero-volume filled bar; no executable historical trade.'}
        for t in old_trades if(t['code'],t['entry_date']) not in fixed_keys]
    new_keys={(r['code'],r['entry_date'])for r in new}
    old_keys={(t['code'],t['entry_date'])for t in old_trades}
    quality=read(RUN/'data_quality.json')
    source_manifest={str(path.relative_to(RUN)):digest(path)
                     for path in sorted((RUN/'sources').rglob('*')) if path.is_file()}
    save(RUN/'source_manifest.json',source_manifest)
    payload={'as_of':AS_OF,'scope':'Saved radar union 2026-05-21 through 2026-09-03; 695 stocks, 8467 records',
        'budget_per_trade':BUDGET,'notional_model':'Theoretical fractional-share units; no portfolio cash constraint',
        'cost_assumption':{'buy_fee':BUY_FEE,'sell_fee':SELL_FEE,'sell_tax':SELL_TAX,'minimum_fee':MIN_FEE},
        'original_v2':old_summary,'fixed_entries':summarize(fixed),'corrected_full_replay':summarize(new),
        'fixed_entry_exclusions':excluded,'new_entry_keys':sorted(new_keys-old_keys),'removed_entry_keys':sorted(old_keys-new_keys),
        'full_trades':new,'fixed_entry_trades':fixed,
        'source_checks':{'candidates':len(quality),'exchange_references':sum(q['exchange_reference_count']for q in quality),
            'reference_mismatch_notes':[{'code':q['code'],'note':n}for q in quality for n in q['notes']if 'mismatch' in n],
            'zero_volume_bars_removed':sum(len(q['zero_volume_dates_removed'])for q in quality)},
        'input_hashes':{str(p.relative_to(ROOT)):digest(p)for p in [SOURCE,RUN/'input_manifest.json',RUN/'backtest.json',RUN/'selection_snapshot.json',
            RUN/'source_manifest.json',RUN/'settlement_terms.json',ROOT/'scripts/course_corporate_action_backtest.py',
            ROOT/'scripts/course_corporate_action_data.py',ROOT/'scripts/course_radar_trigger_backtest.py',
            ROOT/'scripts/course_watchlist_backtest.py',ROOT/'scripts/course_daily_screen_trial.py',Path(__file__)]}}
    save(RUN/'validation.json',{'full_trade_count':len(new),'fixed_trade_count':len(fixed),
        'replay_errors':bt['errors'],'unchanged_thresholds':True,'original_report_hash_unchanged':True,
        'all_entries_within_traded_bar':True,'no_sale_before_share_release':True,
        'no_ex_date_purchase_receives_same_dividend':True,'exits_follow_prior_close_trigger':True,
        'asset_accounting_reconciled':True,'official_reference_mismatches':payload['source_checks']['reference_mismatch_notes']})
    save(RUN/'accounting.json',payload)
    render(payload)
    print({k:payload[k]for k in ['original_v2','fixed_entries','corrected_full_replay','fixed_entry_exclusions','source_checks']})


def render(p):
    f,n,o=p['fixed_entries'],p['corrected_full_replay'],p['original_v2']
    money=lambda x:f'{x:+,.2f}'
    lines=['# 固定樞紐防線：除權息及股利修正後回測','',
      '資料截點：2026-09-04 收盤（不是 9/5 即時行情）。選股：2026-05-21～09-03 已保存雷達聯集，63 日、8,467 筆、695 檔。',
      '', '## 結果','',
      '| 版本 | 筆數 | 主部位已出場／持有 | 每筆一萬元合計損益（未扣成本） | 平均報酬 |',
      '|---|---:|---:|---:|---:|']
    for label,s in [('舊 v2（原報告，尚未修正）',o),('原進場日期固定診斷（89 筆可交易）',f),('同一策略重新逐日判斷（主結果）',n)]:
        lines.append(f"| {label} | {s['trades']} | {s['closed_core_positions']}／{s['open_core_positions']} | {money(s['gross_pnl'])} 元 | {s['gross_return_pct']:+.4f}% |")
    lines += ['',f"主結果：股價／配股權益損益 {money(n['price_pnl'])} 元，加現金股利權益 {money(n['dividend_total'])} 元，合計 {money(n['gross_pnl'])} 元。",
      f"其中已出場主部位組合 {money(n['closed_group_gross_pnl'])} 元；尚持有組合 {money(n['open_group_gross_pnl'])} 元。兩組都包含各自股利，沒有漏掉持有中獲利。",
      f"已到發放日現金股利 {n['dividend_paid']:,.2f} 元；未到期應收 {n['dividend_receivable']:,.2f} 元；發放日未核定 {n['dividend_payment_unverified']:,.2f} 元（只列權益，不列可用現金）。",
      f"買進及已發生賣出估計成本 {n['fees_and_transaction_tax']:,.2f} 元；扣後損益 {money(n['net_pnl'])} 元／{n['net_return_pct']:+.4f}%。若把所有剩餘股票及權益也視作可立即平倉，另有估計成本 {n['estimated_final_liquidation_cost']:,.2f} 元；這只是成本敏感度，不代表未發放配股可交易。",
      f"正報酬 {n['positive_trades']}/{n['trades']}（{n['positive_rate_pct']:.2f}%）；持倉期間平均總權益 MFE（最大浮盈）{n['mean_mfe_pct']:.2f}%。",
      '', '## 方法沒有改成新停利策略','',
      '- 原課程機械判斷、太極／樞紐結構、觸發與不追價門檻、監控 20 根 K 失效條件均沿用；不是 AI 主觀判讀回測。',
      '- 仍為前一根收盤建立進場計畫，下一個可交易日檢查原觸發價與不追價限制；收盤跌破固定防線，下一個可交易日開盤賣出。沒有 MA21、+2R 狀態切換或額外移動停利。',
      '- 除息／配股時只把既有觸發價與固定防線換算到相同經濟口徑。例如原防線 90 元、配息 5 元，換算為 85 元；這不是放寬原始總權益風險。',
      '- 成交使用還原的當日價格；分析歷史只納入截至判斷當日已生效的公司行動。先解除 Yahoo 歷史拆併股正規化，再以交易所除權息前收盤核對。不能用含息還原收盤價再加一次現金股利。',
      '- 現金股利只給除權息前已持有者，不再投入；未到發放日列應收。配股不當成除權日現金，也不假設當天可賣。',
      '- 現金增資依公告比例調整分析價位，但本次不另加資金認購，也不假設出售認股權取得收入。',
      '- 零成交量填補列不能成交，也不當成真正的日 K。原 6113 亞矽 7/10 進場因此無法沿用；原固定進場診斷是 89 筆，不虛構第 90 筆。7/10 颱風休市，相關除權息生效時點順延到下一個可交易日。',
      '', '## 配股結算與限制','',
      '宇峻（3546）配股 8/11、松瑞藥（4167）配股 8/31 可用。若主部位先出場，新配股等可用日開盤才另賣。隆大（5519）8/14 先發新股權利證書、8/28 轉普通股；本資料無證書行情，採持有到 8/28 再賣普通股的明示假設。其餘未核實配股發放日的持倉，配股列權益估值，不冒充可用現金。',
      f"將配股延後處理，相較錯把所有配股在主部位出場日同時賣出的算法，總損益差額 {money(n['stock_entitlement_settlement_impact'])} 元。",
      '', '## 如何解讀','',
      f"每筆 10,000 元是等金額研究口徑，累計進場名目金額 {n['cumulative_entry_notional']:,.0f} 元不是所需最低資金；沒有模擬資金不足、整股／零股成交、盤中撮合、滑價與 T+2 交割資金。允許理論上的小數股，配股畸零股也按市值估值，不是券商可直接照單成交的淨值。",
      '成本只是明示假設：買賣各 0.1425%，每次手續費最低 20 元，賣出稅 0.3%；買進手續費另計，未扣個人股利所得稅、補充保費與匯費。已到支付日也只是依公告視為已付，未接券商實際帳務。',
      '原進場固定診斷只隔離部分帳務影響，不是另一次完整投資組合；修正後持有時間可能重疊。主結果才會重新依逐日狀態決定是否進場。',
      '修正涵蓋行情正規化、除權息權益及無成交列；因此新舊差異不能全部歸因於「多加了股利」。原雷達候選清單不重算，也未修正上游選股當時可能使用的錯誤價位。',
      '資料為目前能取得的歷史快取及公司行動資料，非完整當時版本資料庫。部分減資／面額變更歷史仍採 Yahoo 事件；本次持倉未跨越這類事件。數據供應商量能口徑與日內實際可成交價仍是限制。',
      '短期間、同一批資料的正負報酬不能證明長期穩定獲利；本次未針對結果調整任何停利參數。',
      '', '## 核對來源','',
      f"上市及上櫃官方除權息參考紀錄核對 {p['source_checks']['exchange_references']} 筆，顯著參考價不符 {len(p['source_checks']['reference_mismatch_notes'])} 筆。FinMind 配息政策只用成功取得的快照；額度耗盡後未重試或繞過限制。",
      '',
      '- [TWSE 除權息計算結果](https://www.twse.com.tw/zh/announcement/ex-right/twt49u.html)',
      '- [TPEX 除權息計算結果](https://www.tpex.org.tw/zh-tw/announce/market/ex/cal.html)',
      '- [7/10 股市休市及交割順延公告](https://stock.concords.com.tw/event/event_data_view.aspx?ID=4336)',
      '- 股利支付日與配股到帳日來源保存在 sources/finmind、settlement_terms.json；還原前快取與來源雜湊保存在 input_manifest.json。',
      '', '## 重現與檢查','',
      '在專案根目錄執行（兩個回測／報告指令僅讀取本次凍結資料，不連線）：',
      '', '```powershell',
      'python -X utf8 scripts/course_corporate_action_backtest.py',
      'python -X utf8 scripts/course_corporate_action_report.py',
      'python -X utf8 -m pytest tests/test_course_corporate_action_backtest.py tests/test_course_corporate_action_report.py tests/test_course_radar_trigger_backtest.py -q',
      '```', '',
      'validation.json 記錄逐筆成交價在當日高低價範圍、先收盤觸發再出場、配股未發放不賣、除息日新買不領同次股利，以及現金／應收／股票權益加總核對。原 v2 報告與正式機器人快取沒有覆寫。',
      '', '## 主結果逐筆（未扣成本；包括持有中）','',
      '| 股票 | 進場日 | 進場價 | 目前等值防線 | 主部位出場日 | 狀態 | MFE | 股利權益 | 截點總損益 | 截點報酬 |',
      '|---|---|---:|---:|---|---|---:|---:|---:|---:|']
    for r in p['full_trades']:
        lines.append(f"| {r['code']} {r['name']} | {r['entry_date']} | {r['entry_price']:.4f} | {r['last_equivalent_defense']:.4f} | {r['core_exit_date'] or '—'} | {r['status']} | {r['mfe_pct']:.2f}% | {r['dividend_total']:.2f} | {money(r['gross_pnl'])} | {r['gross_return_pct']:+.2f}% |")
    lines += ['', 'MFE（最大浮盈）為主部位持有期間「股票＋配股權益＋現金股利權益」最高估值，未扣成本；不是可事先知道或保證成交的最高賣點。原報表的純價格 MFE 與此口徑不同。',
              '', '完整現金／應收／配股分錄、原進場固定診斷與來源雜湊見 accounting.json。']
    (RUN/'backtest.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


if __name__=='__main__':build()

"""Descriptive entry-time comparison, not a fitted stock selection rule.

Future observations assign outcomes only. Features end at the pre-entry plan
close, before the intraday trigger. Original 13 cases and revised 17 cases are
kept separate. No trading strategy, cache, or existing backtest is modified.
"""
from __future__ import annotations
from collections import Counter,defaultdict
from datetime import date,timedelta
from functools import lru_cache
from pathlib import Path
import math,sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import pandas as pd
from scripts.course_corporate_action_data import RUN,SOURCE,AS_OF,read,save,digest
from scripts.course_corporate_action_backtest import load_inputs,adjusted_history,book_action,open_trade,wealth
from scripts.course_watchlist_backtest import enlightenment_snapshot

OUT=RUN/'entry_commonality'
ITEMS={i['code']:i for i in read(RUN/'input_manifest.json')['items']}
STOCKS={r['code']:r for r in read(ROOT/'stock_list.json')['stocks']}
SELECTIONS=read(RUN/'selection_snapshot.json')['rows']
SOURCES=set()


@lru_cache(None)
def prices(code):return load_inputs(ITEMS[code])[:2]


def read_source(path):
    SOURCES.add(str(path))
    return read(path)


@lru_cache(None)
def external_series():
    chips=[]
    for p in sorted((ROOT/'.cache/chip_daily').glob('2026*.csv')):
        if '20260401'<=p.stem<='20260903':
            d=pd.read_csv(p,dtype={'code':str});chips.append(d);SOURCES.add(str(p))
    chip=pd.concat(chips,ignore_index=True)
    revenues=defaultdict(list)
    for p in sorted((ROOT/'.cache/monthly_revenue').glob('*_2026*.json')):
        for r in read_source(p):
            month=pd.Timestamp(r['month'])
            # No exact timestamp in most cached records: wait until the 11th
            # of the following month, never use that same month's revenue.
            fallback=(month+pd.offsets.MonthBegin(1)).replace(day=11).date().isoformat()
            pub=r.get('published_at')
            exact=bool(pub and r.get('published_at_source')!='statutory_deadline_fallback')
            available=max(fallback,str(pub)[:10]) if pub else fallback
            revenues[str(r['code'])].append({**r,'available':available,'exact_publication':exact})
    valuations=defaultdict(list)
    for p in sorted((ROOT/'.cache/valuation_history').glob('*_2026*.json')):
        for r in read_source(p).get('rows',[]):
            if r.get('date') and r['date']<=AS_OF:valuations[str(r['code'])].append(r)
    return chip,revenues,valuations


@lru_cache(None)
def tdcc(day):
    paths=[p for p in (ROOT/'.cache/tdcc').glob('2026*.csv') if p.stem<=day.replace('-','')]
    if not paths:return None,None
    p=max(paths,key=lambda p:p.stem);SOURCES.add(str(p))
    f=pd.read_csv(p,dtype={'證券代號':str})
    if '持股分級' not in f:return None,None
    f['證券代號']=f['證券代號'].str.strip()
    f['level']=pd.to_numeric(f['持股分級'],errors='coerce')
    f['ratio']=pd.to_numeric(f['占集保庫存數比例%'],errors='coerce')
    totals=f[f.level.between(12,15)].groupby('證券代號').ratio.sum()
    return p.stem,totals.to_dict()


def future_outcome(trade,raw,actions):
    """20-session opportunity with no exit, separate from actual strategy MFE."""
    bars=raw[raw.date.dt.strftime('%Y-%m-%d')>=trade['entry_date']]
    q=open_trade(trade['code'],trade['name'],trade['entry_date'],trade['entry_price'],trade['initial_defense'],
                 {'armed_date':trade['armed_date']},{'sequence':1,'added_date':trade['monitor_added_date']})
    maximum=0.;hit=None;max20=0.
    mapped={a['date']:a for a in actions}
    for j,(_,b) in enumerate(bars.iterrows(),1):
        day=b.date.date().isoformat()
        if j>1 and day in mapped:book_action(q,mapped[day])
        ret=(wealth(q,b.high)/q['entry_price']-1)*100
        maximum=max(maximum,ret)
        if j<=20:max20=max(max20,ret)
        if hit is None and ret>=20:hit=j
    return {'future_bars_available':len(bars),'first_20pct_bar_without_exit':hit,
            'mfe_first20_without_exit':max20 if len(bars)>=20 else None}


def features(trade):
    code,cutoff=trade['code'],trade['armed_date']
    assert cutoff<trade['entry_date']
    raw,actions=prices(code)
    h=adjusted_history(raw,actions,cutoff)
    assert h.iloc[-1].date.date().isoformat()==cutoff
    s=enlightenment_snapshot(h);b=h.iloc[-1]
    pct=lambda a,z:(a/z-1)*100 if z else None
    prev20=h.iloc[-21:-1];atr=float(b.ATR14)
    prior=[r for r in SELECTIONS if r['code']==code and trade['monitor_added_date']<=r['date']<=cutoff]
    strategies=sorted({x for r in prior for x in r.get('strategy_codes',[])})
    r={'code':code,'name':trade['name'],'entry_date':trade['entry_date'],'feature_cutoff':cutoff,
       'industry':STOCKS.get(code,{}).get('industry'),'market':ITEMS[code]['market'],
       'month':trade['entry_date'][:7],'entry_price':trade['entry_price'],
       'pattern':s['setup']['pattern_code'],'quadrant':s['quadrant']['working_quadrant'],
       'relation':s['direction']['relation'],'sstv_quality':s['sstv']['quality'],
       'lens':s['structural_lens'],'taiji_sequence':s['taiji']['sequence'],'taiji_state':s['taiji']['state'],
       'corrected_armed':s['armed'],'atr_pct':atr/b.close*100,
       'risk_pct':s['risk']['distance_pct'],'extension_atr':s['risk']['extension_atr'],
       'range10_atr':s['sstv']['range_10d_atr'],'tr_cv10':s['sstv']['true_range_cv_10d'],
       'volume_ratio':float(b.volume/prev20.volume.mean()),
       'volume5_20_ratio':float(h.iloc[-5:].volume.mean()/h.iloc[-20:].volume.mean()),
       'average_volume20_lots':float(h.iloc[-20:].volume.mean()/1000),
       'close_vs_ma21_pct':pct(b.close,b.MA21),'close_vs_ma55_pct':pct(b.close,b.MA55),
       'below_prior20high_pct':pct(b.close,prev20.high.max()),
       'return5_pct':pct(b.close,h.iloc[-6].close),'return20_pct':pct(b.close,h.iloc[-21].close),
       'return60_pct':pct(b.close,h.iloc[-61].close),
       'ma21_slope5_pct':pct(b.MA21,h.iloc[-6].MA21),'ma55_slope10_pct':pct(b.MA55,h.iloc[-11].MA55),
       'ma_bull_stack':bool(b.MA21>b.MA55>b.MA105),'close_above_ma21':bool(b.close>b.MA21),
       'close_above_ma55':bool(b.close>b.MA55),'ma21_rising':bool(b.MA21>h.iloc[-6].MA21),
       'prior_selection_dates':len(prior),'strategies':strategies,
       'monitor_calendar_days':(date.fromisoformat(trade['entry_date'])-date.fromisoformat(trade['monitor_added_date'])).days,
       'snapshot':s,'future':future_outcome(trade,raw,actions)}
    chips,revenue,values=external_series()
    dates=list(h.date.dt.strftime('%Y-%m-%d'))
    c=chips[(chips.code==code)&chips.date.isin(dates[-20:])].drop_duplicates('date').sort_values('date')
    for window in [5,20]:
        z=c[c.date.isin(dates[-window:])]
        r[f'chip_{window}_coverage']=len(z)
        for key,field in [('foreign','foreign_net_lots'),('trust','trust_net_lots')]:
            val=float(z[field].sum()) if len(z)==window and z[field].notna().all() else None
            r[f'{key}{window}_net_lots']=val
            volume=h.iloc[-window:].volume.sum()/1000
            r[f'{key}{window}_volume_pct']=None if val is None else val/volume*100
    rev=[v for v in revenue[code] if v['available']<=cutoff]
    rev=sorted({v['month']:v for v in rev}.values(),key=lambda v:v['month'])
    r['revenue_month']=rev[-1]['month'] if rev else None
    r['revenue_yoy']=rev[-1].get('yoy') if rev else None
    r['revenue3_yoy_mean']=sum(v['yoy'] for v in rev[-3:])/3 if len(rev)>=3 and all(v.get('yoy') is not None for v in rev[-3:]) else None
    r['revenue_availability']=rev[-1]['available'] if rev else None
    r['revenue_exact_publication']=rev[-1]['exact_publication'] if rev else None
    v=[v for v in values[code] if v['date']<=cutoff and (date.fromisoformat(cutoff)-date.fromisoformat(v['date'])).days<=45]
    v=max(v,key=lambda v:v['date'])if v else {}
    r.update(valuation_date=v.get('date'),pb=v.get('pb_ratio'),pe=v.get('pe_ratio'),dividend_yield=v.get('dividend_yield_pct'))
    # Weekly TDCC record date != publication timestamp. Lag at least 7 days.
    td,holders=tdcc((date.fromisoformat(cutoff)-timedelta(days=7)).isoformat())
    td_prev,oldholders=tdcc((date.fromisoformat(cutoff)-timedelta(days=35)).isoformat())
    a=holders.get(code)if holders else None;old=oldholders.get(code)if oldholders else None
    r.update(tdcc_record_date=td,large_holder400_pct=a,large_holder400_change_pp=a-old if a is not None and old is not None else None)
    return r


NUMERIC=['entry_price','atr_pct','risk_pct','extension_atr','range10_atr','tr_cv10','volume_ratio','volume5_20_ratio',
         'average_volume20_lots','close_vs_ma21_pct','close_vs_ma55_pct','below_prior20high_pct','return5_pct','return20_pct','return60_pct',
         'ma21_slope5_pct','ma55_slope10_pct','prior_selection_dates','monitor_calendar_days',
         'foreign5_volume_pct','trust5_volume_pct','foreign20_volume_pct','trust20_volume_pct',
         'revenue_yoy','revenue3_yoy_mean','pb','pe','dividend_yield','large_holder400_pct','large_holder400_change_pp']
CATEGORICAL=['pattern','quadrant','relation','sstv_quality','lens','taiji_sequence','taiji_state','industry','market','month',
             'ma_bull_stack','close_above_ma21','close_above_ma55','ma21_rising','corrected_armed']


def comparison(rows,target='target'):
    groups=[[r for r in rows if r[target]],[r for r in rows if not r[target]]]
    out={'group_sizes':list(map(len,groups)),'numeric':{},'categorical':{}}
    for key in NUMERIC:
        out['numeric'][key]=[]
        for g in groups:
            a=[r[key]for r in g if r.get(key) is not None and math.isfinite(r[key])]
            out['numeric'][key].append({'n':len(a),'median':float(pd.Series(a).median())if a else None})
    for key in CATEGORICAL:out['categorical'][key]=[dict(Counter(str(r[key])for r in g)) for g in groups]
    return out


def condition_counts(rows,key,test):
    groups=[[r for r in rows if r['target']],[r for r in rows if not r['target']]]
    return [(sum(test(r[key])for r in g if r.get(key)is not None),sum(r.get(key)is not None for r in g))for g in groups]


def render(payload):
    rows=payload['datasets']['original89'];new=payload['datasets']['corrected104']
    c=payload['comparisons']['original89'];cn=payload['comparisons']['corrected104']
    chosen=sorted([r for r in rows if r['target']],key=lambda r:(r['entry_date'],r['code']))
    fmt=lambda x:'—' if x is None else f'{x:.2f}'
    labels={'RECLAIM':'收復均線','PULLBACK_RELAUNCH':'多頭回檔再發動'}
    lines=['# 原 13 檔高 MFE 股票：觸發前的共同特徵','',
      '這是描述性、探索性比較，不是新選股規則、AI 盲測或獨立樣本驗證。沒有修改交易程式或任何回測結果。',
      '', '## 樣本與時間口徑','',
      '- 主分析：舊 v2 的 13 檔 MFE ≥20% 案例，對照原名單其餘 76 筆可交易案例。7/10 亞矽休市虛擬成交剔除，因此母體是 89 筆，不是 90 筆。',
      '- 同時檢查除權息修正版 104 筆（17 筆 MFE ≥20%、87 筆其餘）。兩組大量重疊，只是敏感度檢查，不能當獨立驗證。',
      '- 預測特徵截在各筆進場前 ARMED（建立進場計畫）日收盤；不用觸發日的最終成交量、收盤價或觸發後新聞。進場價格是執行時已知價格。',
      '- 價量先沿用上一版除權息校正，再依各截點計算；MFE 使用之後資料作結果標籤，沒有放進特徵。',
      '- 旺旺保屬原 13 檔，但修正後同日已不符合完整進場條件；因此新版 17 檔不包括該筆。本表保留它以回答原 13 檔的問題。',
      '', '## 可提出的解釋','',
      '最接近的描述是「既有多頭中的回檔／收復」，其中部分較低淨值比、已有外資承接。不是 13 檔都有一組獨特密碼，也不是已證明這些因素造成上漲。',
      '', '| 觸發前特徵 | 原 13 檔 | 其餘 76 筆 | 新版 17 檔／其餘 87 筆 |',
      '|---|---:|---:|---:|']
    metrics=[('股價淨值比中位數','pb'),('外資近 5 日淨買超／同期成交量中位數（%）','foreign5_volume_pct'),
             ('400 張以上持股占比中位數（%）','large_holder400_pct'),
             ('前 20 日漲幅中位數（%）','return20_pct'),('準備日成交量／前 20 日均量中位數','volume_ratio'),
             ('最新可用月營收 YoY 中位數（%）','revenue_yoy'),
             ('最近 3 月營收 YoY 平均值之中位數（%）','revenue3_yoy_mean'),
             ('距離 MA21 的 ATR 倍數中位數','extension_atr')]
    for title,key in metrics:
        a,b=c['numeric'][key];x,y=cn['numeric'][key]
        suffix='（12／75 筆有完整資料）' if key=='foreign5_volume_pct' else ''
        lines.append(f"| {title}{suffix} | {fmt(a['median'])} | {fmt(b['median'])} | {fmt(x['median'])}／{fmt(y['median'])} |")
    lines += ['', '### 1. 型態共同，但幾乎也是整套規則的共同條件','',
      '原 13 檔中 9 檔為收復均線，4 檔為多頭回檔再發動，沒有一檔屬於當根收盤突破 20 日新高。13 檔都在 MA55 上方，12 檔在 MA21 上方。',
      '但對照組也有 70/76 筆屬於收復／回檔，74/76 筆在 MA55 上方，72/76 筆在 MA21 上方。所以這是入場背景，不是足以分辨大漲股的新證據。',
      '太極並沒有單一共同段落：原 13 檔分布於第 2、3、4、5 段及未辨識狀態；百容、太空梭、華興在機械分類中是第 5 段。不能把「只買某一段」當成這次已獲支持的結論。',
      '', '### 2. 低淨值比是一條相對值得追查的線索，但遠非必要或充分條件','',
      '7/13 檔的歷史 PB <1；對照組為 20/76。若把 PB <1 當硬門檻，原 89 筆會留下 27 筆，只有 7 筆屬原高 MFE 案例，並漏掉另外 6 檔，包括聯穎、華興、元大金。',
      '這比較像「部分估值修復／傳產金融回升」假說，不等於便宜就會漲；PB、股價、大戶持股也受產業及公司規模影響。沒有完成產業配對或市場基準歸因。',
      '', '### 3. 外資承接有差異，但不是每個月份都成立','',
      '外資 5 日淨買超占比 = 最近 5 個已完成交易日外資淨買超股數 ÷ 同期總成交股數。原案例有 12 檔資料完整，其中 10 檔為正；對照為 44/75。投信的兩組中位數均接近零，沒有同樣清楚的差異。',
      '5 月原高 MFE 組的外資占比中位數 19.34%，同月對照 3.22%；但 6 月分別為 −0.64%／+1.60%，方向反轉。和成、長榮航進場前外資仍是淨賣超，是明確反例。',
      '大戶持股「水準」略高，但「近期持股增加」並未顯示相同優勢。原 13 檔僅 5 檔有完整約四週差分，不能宣稱大戶持續加碼是共同原因。',
      '', '### 4. 爆量、高成長、完美整理都不是必要條件','',
      '原 13 檔只有華興一檔在準備日量比 ≥1.5；9 檔的近 5 日均量低於近 20 日均量，對照組也有 54/76，差異有限。原 13 檔的量比差異在新版 17 檔中幾乎消失。',
      '原 13 檔月營收 12 檔為正成長，但對照也有 65/76。營收 YoY 中位數反而低於對照組，不能說它們是營收成長最強的股票。金融業營收口徑與製造業不同，不應直接混合排名。',
      'SSTV（整理品質）合格率約相同：7/13 對 42/76；10 日真實波幅變異係數兩組中位數都是 0.357。不能說它們的整理型態全都明顯優於其他交易。',
      '', '## 時期、持有長度與挑選偏誤','',
      '原 13 檔中 7 檔在 5 月進場；同月原有效交易總共 14 筆。高 MFE 明顯集中於同一段市場時間，不能把全部差異都歸因於股票本身。監控到觸發較快看似有利，但 5、6 月內部分組並不支持，可能是起始名單與日期分布造成。',
      '另做同樣 20 個交易日觀察窗：假設入場後保持原始權益，不理會出場規則，只比較 20 日內是否曾達 +20%。不足 20 日資料者整筆排除；這只比較進場後的價格機會，不是交易策略績效。',
      '', '| 同 20 日觀察窗 | 達 +20%／未達 | 外資占比中位數（%） | PB 中位數 |',
      '|---|---:|---:|---:|']
    for key,title in [('original89_fixed20','原有效交易'),('corrected104_fixed20','新版交易')]:
        c20=payload['comparisons'][key];a,b=c20['numeric']['foreign5_volume_pct'];x,y=c20['numeric']['pb']
        lines.append(f"| {title} | {c20['group_sizes'][0]}／{c20['group_sizes'][1]} | {fmt(a['median'])}／{fmt(b['median'])} | {fmt(x['median'])}／{fmt(y['median'])} |")
    lines += ['', '外資與 PB 的方向仍在，但這些仍是同一批樣本內的探索，不能當顯著性、因果關係或未來命中率。已經依結果挑出 13 檔再尋找差異，本身就存在挑選偏誤；本次沒有訓練模型、搜尋最佳參數或證明樣本外報酬。',
      '', '## 原 13 檔逐筆特徵','',
      '| 股票 | 進場日 | 特徵截點 | 型態 | 原 MFE／校正 MFE | 外資 5 日占比 | 歷史 PB | 營收月份／YoY |',
      '|---|---|---|---|---:|---:|---:|---:|']
    old={(t['code'],t['entry_date']):t for x in read(SOURCE)['candidates']for t in x['trades']}
    for r in chosen:
        prior=old[(r['code'],r['entry_date'])]['mfe_pct']
        lines.append(f"| {r['code']} {r['name']} | {r['entry_date']} | {r['feature_cutoff']} | {labels[r['pattern']]} | {prior:.2f}%／{r['mfe_pct']:.2f}% | {fmt(r['foreign5_volume_pct'])}% | {fmt(r['pb'])} | {r['revenue_month'][:7]}／{fmt(r['revenue_yoy'])}% |")
    lines += ['', '校正 MFE 欄沿用「固定原進場日期診斷」，並非新版所有案例的新進場點。PB 有些僅有最近月末資料；確切估值日期、量能、各段太極、法人／集保資料日均保存在 analysis.json。',
      '', '## 資料可靠度與排除內容','',
      '- 價量與課程結構：沿用凍結行情及除權息來源，按每筆觸發前日期切片。',
      '- 法人：本機每日官方／FinMind 歷史快取，缺任何一個所需交易日就列缺資料，不能當成零買賣超。',
      '- 月營收：多數沒有確切公告時間。保守採次月 11 日以後才能使用；數值仍是目前快取的歷史版本，並非完整當時版本資料庫。',
      '- 集保：資料日期不是公告時間，使用至少落後 7 天的週資料；400 張以上為持股級距 12～15。',
      '- 歷史估值：只用截點以前、45 日內的官方歷史 PB／PE；不是今天 PB 倒套過去。不同產業估值不能視為完全可比。',
      '- 季財報逐項公告時間、完整公司新聞與產業事件沒有可靠的全樣本當時版本，因此不能聲稱已涵蓋所有基本面原因。',
      '- 舊雷達總分／新聞不列預測特徵：例如 5/21 百容候選 JSON 出現 5 月整月營收及 39.65 的 price，同檔技術訊號卻為當日收盤 24.6；新聞也包含他股標題。路徑日期不等於內容可用時間。',
      '- 上游候選名單本身是否受這些時間不一致影響，尚未重新建立逐日資料版本驗證。因此這份探索不能視為整個策略已通過無前視偏誤審核。',
      '', '## 解讀與後續用途','',
      '可把「回檔／收復背景、外資承接、相對低 PB、產業／大盤時段」列成 AI 解讀欄位，但目前不宜直接變成一刀切門檻或 Top 5 排名。先保留反例、漏抓案例與缺資料標記，再在新的日期名單驗證。',
      '高 MFE 不等於已實現獲利：聯穎、太空梭、華興、萬在仍是固定防線版中的虧損案例。改善進場識別與改善浮盈保留是兩個不同問題。',
      '', '## 來源與重現','',
      f'- [修正後帳務]({(RUN/"accounting.json").as_posix()})、[修正後回放]({(RUN/"backtest.json").as_posix()})、[舊版回測]({SOURCE.as_posix()})。',
      '- analysis.json 保存完整兩組特徵、月份分組、固定觀察窗、來源檔案雜湊及限制；原始機器人與舊報告沒有修改。',
      '- [證交所：月營收資料查詢及申報時點](https://www.twse.com.tw/staticFiles/news/event/event_download_201501141045_02.pdf)',
      '- [Bailey 等：回測過度擬合與事後挑選問題](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)',
      '', '```powershell', 'python -X utf8 scripts/course_mfe_commonality_analysis.py', '```']
    (OUT/'analysis.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def build():
    bt=read(RUN/'backtest.json');old=read(SOURCE);account=read(RUN/'accounting.json')
    old_targets={(t['code'],t['entry_date'])for c in old['candidates']for t in c['trades']if t['mfe_pct']>=20}
    sets={'original89':bt['fixed_original_entries'],'corrected104':[t for c in bt['candidates']for t in c['trades']]}
    cache={};datasets={}
    for label,trades in sets.items():
        rows=[]
        for t in trades:
            key=(t['code'],t['entry_date'],t['armed_date'],t['entry_price'])
            if key not in cache:cache[key]=features(t)
            r={**cache[key],'mfe_pct':t['mfe_pct'],'performance_return_pct':t['performance_return_pct']}
            r['target']=(t['code'],t['entry_date'])in old_targets if label=='original89'else t['mfe_pct']>=20
            rows.append(r)
        datasets[label]=rows
    results={label:comparison(rows) for label,rows in datasets.items()}
    # Calendar stratification mitigates different exposure / market-period mix.
    monthly={label:{m:comparison([r for r in rows if r['month']==m]) for m in sorted({r['month']for r in rows})}
             for label,rows in datasets.items()}
    for label,rows in datasets.items():
        mature=[{**r,'fixed20_target':r['future']['mfe_first20_without_exit']>=20} for r in rows if r['future']['mfe_first20_without_exit'] is not None]
        results[label+'_fixed20']=comparison(mature,'fixed20_target')
    payload={'method':'Descriptive, no threshold optimization; pre-entry close only',
         'as_of':AS_OF,'original_target_count':len(old_targets),'datasets':datasets,'comparisons':results,'monthly':monthly,
         'sources':{p:digest(p)for p in sorted(SOURCES)},
         'source_limitations':['Radar candidate aggregate scores/news contain temporal inconsistencies and are not predictors.',
           'Revenue release dates mostly inferred: conservative next-month 11th, current-vintage historical values.',
           'TDCC has record dates, not release timestamps: minimum seven-day lag.',
           'Valuation has dated historical observations, may lag up to 45 days.',
           'Original 13 and corrected 17 are overlapping in-sample cohorts, not independent validation.',
           'Fixed20 opportunity ignores exits for equal horizon; not the original strategy profit.']}
    save(OUT/'analysis.json',payload)
    render(payload)
    print('groups',{k:v['group_sizes']for k,v in results.items()})
    for k,v in results['original89']['numeric'].items():print(k,[(a['n'],round(a['median'],3)if a['median'] is not None else None)for a in v])
    print('categorical',results['original89']['categorical'])


if __name__=='__main__':build()

"""Deterministic checks for explicit EPS x PE valuation tables; never rewrites prices."""
from __future__ import annotations

import re


def _numbers(cell):
    if re.search(r"資料不足|缺失|待核|未提供|不適用",cell):
        return []
    text=re.sub(r"\b20\d{2}(?:[EeFf]|年|Q[1-4])", "", cell).replace(",", "")
    values=[float(v) for v in re.findall(r"(?<![\w.])\d+(?:\.\d+)?",text)]
    return values if 0 < len(values) <= 2 else []


def validate_valuation_tables(markdown):
    errors=[]
    checked=0
    columns=None
    for line in markdown.splitlines():
        if not line.strip().startswith("|"):
            columns=None
            continue
        cells=[c.strip() for c in line.strip().strip("|").split("|")]
        if any("EPS" in c.upper() for c in cells) and any("本益比" in c for c in cells):
            eps=[i for i,c in enumerate(cells) if "EPS" in c.upper()]
            pe=[i for i,c in enumerate(cells) if "本益比" in c]
            price=[i for i,c in enumerate(cells) if any(label in c for label in ("合理股價", "股價區間", "估值下限", "估值上限"))]
            columns=(eps,pe,price) if price else None
            continue
        if columns is None or max(i for group in columns for i in group)>=len(cells) or all(re.fullmatch(r"[-: ]+",c) for c in cells):
            continue
        values=[]
        for group in columns:
            numbers=[]
            for i in group:
                # A separately stated target is not another endpoint of the EPS x PE range.
                numbers.extend(_numbers(re.split(r'[；;]',cells[i])[0]))
            values.append(numbers)
        eps,pe,price=values
        if not eps or not pe or not price:
            continue
        expected=(min(eps)*min(pe),max(eps)*max(pe))
        actual=(min(price),max(price))
        checked+=1
        if any(abs(a-b)>max(1,abs(b)*.005) for a,b in zip(actual,expected)):
            errors.append(f"估值表「{cells[0]}」EPS×本益比應為 {expected[0]:g}–{expected[1]:g}，表列 {actual[0]:g}–{actual[1]:g}；若另有折溢價，須分欄列明。")
    return {"checked_rows":checked,"errors":errors,"passed":not errors}


def validate_valuation_repair(original, repaired):
    result=validate_valuation_tables(repaired)
    before=validate_valuation_tables(original)
    if result['checked_rows']<before['checked_rows']:
        result['errors'].append('數值修正移除了可驗算的估值列，不接受以刪表避開檢查。')
    headings=lambda text:set(re.findall(r'^#{1,6}\s+(.+?)\s*$',text,re.M))
    if headings(original)-headings(repaired):
        result['errors'].append('數值修正遺失原報告章節，保留原稿並標示未完整通過。')
    result['passed']=not result['errors']
    return result

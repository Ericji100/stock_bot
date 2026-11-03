"""Deterministic checks for explicit EPS x PE valuation tables; never rewrites prices."""
from __future__ import annotations

import re


def _numbers(cell):
    if re.search(r"資料不足|缺失|待核|未提供|不適用",cell):
        return []
    text=re.sub(r"\b20\d{2}(?:[EeFf]|年|Q[1-4])", "", cell).replace(",", "")
    values=[float(v) for v in re.findall(r"(?<![\w.])[-+]?\d+(?:\.\d+)?",text)]
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
        if any("EPS" in c.upper() for c in cells) and any("本益比" in c or c.strip('*').upper() == 'PE' for c in cells):
            eps=[i for i,c in enumerate(cells) if "EPS" in c.upper()]
            pe=[i for i,c in enumerate(cells) if "本益比" in c or c.strip('*').upper() == 'PE']
            price=[i for i,c in enumerate(cells) if c.strip('*') in ('估值', '估值區間', '隱含價格') or any(label in c for label in ("合理股價", "股價區間", "估值下限", "估值上限"))]
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
        products=[e*p for e in eps for p in pe]
        expected=(min(products),max(products))
        actual=(min(price),max(price))
        checked+=1
        if any(abs(a-b)>max(1,abs(b)*.005) for a,b in zip(actual,expected)):
            errors.append(f"估值表「{cells[0]}」EPS×本益比應為 {expected[0]:g}–{expected[1]:g}，表列 {actual[0]:g}–{actual[1]:g}；若另有折溢價，須分欄列明。")
    # The same scenarios may be columns instead of rows. Transpose only explicit
    # EPS/PE/price matrices, never infer values from arbitrary financial tables.
    for block in re.findall(r"(?:^\s*\|[^\n]+\n?)+", markdown, re.M):
        rows=[[c.strip().strip('*') for c in line.strip().strip('|').split('|')]
              for line in block.splitlines() if line.strip().startswith('|')]
        rows=[r for r in rows if not all(re.fullmatch(r'[-: ]+',c) for c in r)]
        if not rows:
            continue
        eps_columns=[i for i,c in enumerate(rows[0]) if c.upper() == 'EPS']
        multiples={i:float(match.group(1)) for i,c in enumerate(rows[0])
                   if (match := re.fullmatch(r'[×xX]\s*(\d+(?:\.\d+)?)\s*倍', c))}
        if len(eps_columns) == 1 and multiples:
            for row in rows[1:]:
                if len(row) <= max([*multiples, eps_columns[0]]):
                    continue
                eps_values=_numbers(row[eps_columns[0]])
                if len(eps_values) != 1:
                    continue
                for col, multiple in multiples.items():
                    prices=_numbers(row[col])
                    if len(prices) != 1:
                        continue
                    expected=eps_values[0]*multiple
                    checked+=1
                    if abs(prices[0]-expected)>max(1,abs(expected)*.005):
                        errors.append(f'估值矩陣「{row[0]}」EPS×{multiple:g} 應為 {expected:g}，表列 {prices[0]:g}。')
        if any('EPS' in c.upper() for c in rows[0]):
            continue
        eps_rows=[r for r in rows[1:] if 'EPS' in r[0].upper() or '每股盈餘' in r[0]]
        pe_rows=[r for r in rows[1:] if '本益比' in r[0]]
        price_rows=[r for r in rows[1:] if any(k in r[0] for k in ('合理股價','股價區間','估值下限','估值上限'))]
        if not eps_rows or not pe_rows or not price_rows:
            continue
        selected=[*eps_rows,*pe_rows,*price_rows]
        header='| 情境 | '+' | '.join('EPS '+r[0] if r in eps_rows else r[0] for r in selected)+' |'
        transposed=[header,'|'+'---|'*(len(selected)+1)]
        for col in range(1,min(len(r) for r in [rows[0],*selected])):
            transposed.append('| '+rows[0][col]+' | '+' | '.join(r[col] for r in selected)+' |')
        result=validate_valuation_tables('\n'.join(transposed))
        checked+=result['checked_rows']
        errors.extend(result['errors'])
    return {"checked_rows":checked,"errors":errors,"passed":not errors}


def validate_score_formulas(markdown):
    errors = []
    checked = 0
    for match in re.finditer(r'評分公式[：:]([^\n]+)', markdown):
        line = match.group(1).replace('**', '')
        parts = re.split(r'[＝=]', line, maxsplit=1)
        claimed = re.search(r'[。；;]\s*(?:滿分\s*|正向構面合計上限為\s*)(\d+)', line)
        if len(parts) != 2 or not claimed:
            continue
        formula = re.split(r'[。；;]', parts[1])[0]
        terms = re.split(r'([+＋−\-－])', formula)
        sign = '+'
        upper = 0
        count = 0
        complete = True
        for term in terms:
            if term in ('+', '＋', '−', '-', '－'):
                sign = term
                continue
            cap = re.search(r'[（(]滿分\s*(\d+)[）)]', term)
            if not cap and sign in ('−', '-', '－'):
                cap = re.search(r'扣分[^（(]*[（(]扣分項[，,]\s*0[–~～-](\d+)\s*分[）)]', term)
            if not cap:
                complete = False
                break
            count += 1
            if sign in ('+', '＋'):
                upper += int(cap.group(1))
        if not complete or count < 2:
            continue
        checked += 1
        if upper != int(claimed.group(1)):
            errors.append(f'評分公式正向構面上限合計 {upper}，卻宣稱滿分 {claimed.group(1)}；扣分項不會提高上限。請一致說明總分尺度並驗算各股，不變更本地評分規則。')
    return {'checked_formulas': checked, 'errors': errors, 'passed': not errors}


def validate_report_numbers(markdown, sources=()):
    from .financial_unit_validation import validate_cited_monetary_units
    result = validate_valuation_tables(markdown)
    units = validate_cited_monetary_units(markdown, sources)
    result['monetary_units'] = units
    result['errors'].extend(units['errors'])
    scores = validate_score_formulas(markdown)
    result['score_formulas'] = scores
    result['errors'].extend(scores['errors'])
    result['passed'] = not result['errors']
    return result


def numeric_repair_prompt(markdown, errors):
    return ('請只修正下列報告的數值一致性，保留原有所有章節標題、來源編號、網址、風險、事實與情境假設。'
            '不得新編事實，不得刪除數值或證據避開檢查，不得以本地規則取代投研結論。'
            '估值的 EPS×本益比上下限須精確，另行折溢價請分欄。金額先核對期間、幣別、原始單位；'
            'billion 是十億，million 是百萬，不可直接照抄尾數改成億元。只輸出完整正式報告。\n'
            + '\n'.join(errors) + '\n\n' + markdown)


def validate_valuation_repair(original, repaired, sources=()):
    result=validate_report_numbers(repaired, sources)
    before=validate_report_numbers(original, sources)
    if result['checked_rows']<before['checked_rows']:
        result['errors'].append('數值修正移除了可驗算的估值列，不接受以刪表避開檢查。')
    if result['monetary_units']['checked_amounts'] < before['monetary_units']['checked_amounts']:
        result['errors'].append('數值修正減少了可核對的引用金額，保留原稿供檢閱，不接受以刪除數字避開檢查。')
    if result['score_formulas']['checked_formulas'] < before['score_formulas']['checked_formulas']:
        result['errors'].append('數值修正移除了明確評分公式，不接受以刪除公式避開檢查。')
    headings=lambda text:set(re.findall(r'^#{1,6}\s+(.+?)\s*$',text,re.M))
    if headings(original)-headings(repaired):
        result['errors'].append('數值修正遺失原報告章節，保留原稿並標示未完整通過。')
    references=lambda text:set(re.findall(r'\[S\d+\]|https?://[^\s|<>]+', text))
    if references(original)-references(repaired):
        result['errors'].append('數值修正移除了原稿來源引用，不接受以刪除證據修正算式。')
    result['passed']=not result['errors']
    return result

"""Deterministic checks for explicit EPS x PE valuation tables; never rewrites prices."""
from __future__ import annotations

import re
import ast
import operator
import math


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
        if any("EPS" in c.upper() or '每股盈餘' in c for c in cells) and any("本益比" in c or c.strip('*').upper() in {'PE', 'P/E'} for c in cells):
            eps=[i for i,c in enumerate(cells) if "EPS" in c.upper() or '每股盈餘' in c]
            pe=[i for i,c in enumerate(cells) if "本益比" in c or c.strip('*').upper() in {'PE', 'P/E'}]
            price=[i for i,c in enumerate(cells) if c.strip('*') in ('估值', '估值區間', '隱含價格') or any(label in c for label in ("合理股價", "合理價", "目標價", "股價區間", "估值下限", "估值上限"))]
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


FINANCIAL_BINDING_FIELDS = {
    '營業現金流': ('Operating_Cash_Flow', 'operating_cash_flow'),
    '資本支出': ('Capital_Expenditure', 'capital_expenditure'),
    '自由現金流': ('Free_Cash_Flow', 'free_cash_flow'),
    '應付帳款': ('Accounts_Payable', 'accounts_payable'),
    '實收資本': ('Paid_In_Capital', 'paid_in_capital'),
    '每股盈餘': ('EPS', 'eps'),
}


def financial_fact_bindings(data):
    """A named, mechanical cross-reference; never substitutes for the full data."""
    def unwrap(value):
        if isinstance(value, dict) and value.get('資料型態') == '完整分段清單':
            return [row for part in value.get('段落', []) for row in part.get('資料', [])]
        if isinstance(value, dict) and 'payload' in value:
            return unwrap(value['payload'])
        return value
    data = data or {}
    rows = unwrap(data.get('financial_data'))
    if not isinstance(rows, list) or not rows:
        core = ((data.get('high_model_input_package') or {}).get('command_specific_data') or {}).get('payload') or {}
        rows = unwrap(core.get('financial_data'))
    result = []
    for row in rows if isinstance(rows,list) else []:
        if not isinstance(row,dict):
            continue
        values = {label: row[key] for label,keys in FINANCIAL_BINDING_FIELDS.items()
                  if (key := next((k for k in keys if row.get(k) is not None), None)) is not None}
        if values:
            result.append({'公司代號': (data.get('stock') or {}).get('code'),
                '期間': row.get('Quarter') or row.get('period'), '原始欄位數值': values,
                '每股盈餘期間口徑': row.get('EPS_Period_Basis') or 'unverified',
                '幣別': row.get('Currency') or row.get('currency') or '未核實',
                '規則': '數值與正負號照原欄位；千、萬、億轉換須列單位。應付帳款不等於營業現金流。資本支出若為負值，代表現金流出；自由現金流應以營業現金流加上帶符號的資本支出核對，或減去支出絕對值，不可重複減負數。'})
    return result


def validate_local_financial_claims(markdown, data):
    """Check explicit same-line local-period claims, not external guidance or scenarios."""
    by_period = {row['期間']:row['原始欄位數值'] for row in financial_fact_bindings(data)}
    errors, checked = [], 0
    for line in markdown.splitlines():
        if not re.search(r'本地.{0,20}(?:資料|底稿|財報)|具名原始欄位|原始財務欄位',line):
            continue
        periods = set(re.findall(r'20\d{2}Q[1-4]',line))
        if len(periods) != 1 or (period := next(iter(periods))) not in by_period:
            continue
        for label, expected in by_period[period].items():
            if label == '每股盈餘' or not isinstance(expected,(int,float)):
                continue
            match = re.search(re.escape(label)+r'(?:量)?\s*(?:具名原始值(?:為)?|原始值(?:為)?|為|[:：])?\s*([+\-−－]?\s*\d[\d,]*(?:\.\d+)?)\s*(千|萬|億)?元',line)
            if not match:
                continue
            value = float(match[1].replace(',','').replace(' ','').replace('−','-').replace('－','-')) * {None:1,'千':1000,'萬':10000,'億':100000000}[match[2]]
            compared = abs(value) if label == '資本支出' else value
            target = abs(expected) if label == '資本支出' else expected
            checked += 1
            if abs(compared-target) > max(1,abs(target)*.005):
                errors.append(f'本地 {period}「{label}」原欄位為 {expected:g}，文中換算後為 {value:g}；可能混到其他欄位或正負號。請核對具名原始數值，不把應付帳款當現金流，也不捏造原資料矛盾。')
        values = by_period[period]
        ocf, capex, fcf = (values.get(key) for key in ('營業現金流','資本支出','自由現金流'))
        if all(isinstance(v,(int,float)) for v in (ocf,capex,fcf)) and capex < 0:
            false_difference = any(
                re.search(r'(?:OCF|營業現金流)\s*[－−-]\s*(?:Capex|資本支出)',sentence,re.I)
                and re.search(r'差額|仍差|勾稽差|不一致',sentence)
                and not re.search(r'不存在.{0,8}差額|差額.{0,12}不存在|無原稿.{0,20}差額|不是.{0,20}差額',sentence)
                for sentence in line.split('。'))
            if abs(ocf+capex-fcf) <= max(1,abs(fcf)*.005) and false_difference:
                errors.append(f'本地 {period} 帶符號現金流可勾稽：{ocf:g} + ({capex:g}) = {fcf:g}。資本支出原值已為流出負數，不可再減負數製造差額；保留官方正文未取得的限制，但不得捏造資料矛盾。')
    return {'checked_claims':checked,'errors':errors,'passed':not errors}


def validate_report_numbers(markdown, sources=(), structured_data=None):
    from .financial_unit_validation import validate_cited_monetary_units
    result = validate_valuation_tables(markdown)
    units = validate_cited_monetary_units(markdown, sources)
    result['monetary_units'] = units
    result['errors'].extend(units['errors'])
    scores = validate_score_formulas(markdown)
    result['score_formulas'] = scores
    result['errors'].extend(scores['errors'])
    arithmetic = validate_explicit_arithmetic(markdown)
    result['explicit_arithmetic'] = arithmetic
    result['errors'].extend(arithmetic['errors'])
    bindings = validate_local_financial_claims(markdown, structured_data)
    result['financial_bindings'] = bindings
    result['errors'].extend(bindings['errors'])
    rate_unit_errors = [f'比率誤寫金額：{match.group(0)}。請回查原文，區分毛利／營業利益金額與毛利率／營益率百分比，不自行換算或捏造比率。'
        for match in re.finditer(r'(?:毛利率|營益率|營業利益率|淨利率)\s*(?:為|[:：])?\s*[+\-]?\d[\d,]*(?:\.\d+)?\s*(?:千|仟|萬|億)?元',markdown)]
    result['errors'].extend(rate_unit_errors)
    result['rate_unit_errors'] = rate_unit_errors
    checked = result['checked_rows'] + units['checked_amounts'] + scores['checked_formulas'] + arithmetic['checked_formulas'] + bindings['checked_claims']
    result['verification_status'] = 'checked_subset' if checked else 'not_checked'
    result['warnings'] = ([] if checked else ['未識別可驗算的表列或明確算式；0 筆檢查不代表所有數值已驗證。'])
    result['passed'] = not result['errors']
    return result


def validate_explicit_arithmetic(markdown):
    errors, checked = [], 0
    number = r'[+\-]?\d[\d,]*(?:\.\d+)?(?:\s*%)?'
    term = number + r'(?:\s*[×x*÷/]\s*' + number + r')*'
    expr = term + r'(?:\s*[+\-]\s*' + term + r')*'
    equation = re.compile(r'(?P<lhs>'+expr+r')\s*=\s*(?P<rhs>'+expr+r')')
    binary={ast.Add:operator.add,ast.Sub:operator.sub,ast.Mult:operator.mul,ast.Div:operator.truediv}

    def evaluate(text):
        def visit(node):
            if isinstance(node,ast.Constant) and type(node.value) in (int,float):
                return node.value
            if isinstance(node,ast.BinOp) and type(node.op) in binary:
                return binary[type(node.op)](visit(node.left),visit(node.right))
            if isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.UAdd,ast.USub)):
                return visit(node.operand) * (-1 if isinstance(node.op,ast.USub) else 1)
            raise ValueError('Not arithmetic')
        if len(text)>2000:
            raise ValueError('Expression too long')
        expression=text.replace(',','').replace('×','*').replace('x','*').replace('÷','/').strip()
        expression=re.sub(r'([+\-]?\d+(?:\.\d+)?)\s*%',r'(\1/100)',expression)
        value=visit(ast.parse(expression,mode='eval').body)
        if not math.isfinite(value):
            raise ValueError('Non-finite result')
        return value

    for line in markdown.splitlines():
        line=line.replace('**','').translate(str.maketrans({'＋':'+','−':'-','－':'-','＝':'='}))
        position=0
        while match := equation.search(line,position):
            position=match.start('rhs')
            if not re.search(r'[×x*÷/+\-]',match['lhs'].lstrip('+-')):
                continue
            try:
                expected,actual=evaluate(match['lhs']),evaluate(match['rhs'])
            except (ValueError,SyntaxError,ZeroDivisionError,OverflowError,RecursionError):
                continue
            checked+=1
            tolerance=.0002 if '%' in match.group(0) else .02
            if abs(actual-expected)>max(tolerance,abs(expected)*.001):
                errors.append(f'明確算式 {match.group(0)} 不一致，左式應為 {expected:g}、右式為 {actual:g}；不可刪除算式避開檢核。')
    return {'checked_formulas': checked, 'errors': errors, 'passed': not errors}


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

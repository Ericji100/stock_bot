"""Narrow citation-anchored checks for explicit monetary scale conversion errors."""
from __future__ import annotations

from decimal import Decimal
import re


_NUMBER = r'\d+(?:,\d{3})*(?:\.\d+)?'
_ENGLISH = re.compile(
    rf'(?P<currency>NT\$|US\$|TWD\s*|USD\s*)\s*(?P<number>{_NUMBER})\s*'
    r'(?P<unit>trillion|billion|million)\b', re.I)
_CHINESE = re.compile(
    rf'(?P<prefix>新台幣|新臺幣|美元|美金|人民幣|港幣)?\s*'
    rf'(?P<number>{_NUMBER})\s*(?P<unit>百萬|兆|億|萬)'
    r'(?P<suffix>美元|美金|人民幣|港元|元)')
_SCALES = {'million': Decimal(10)**6, 'billion': Decimal(10)**9,
           'trillion': Decimal(10)**12, '百萬': Decimal(10)**6,
           '萬': Decimal(10)**4, '億': Decimal(10)**8, '兆': Decimal(10)**12}


def _amounts(text):
    text = text.replace('**', '')
    for pattern in (_ENGLISH, _CHINESE):
        for match in pattern.finditer(text):
            item = match.groupdict()
            currency = (item.get('currency') or item.get('prefix') or item.get('suffix') or '').strip().upper()
            currency = {'NT$': 'TWD', '新台幣': 'TWD', '新臺幣': 'TWD', '元': 'TWD',
                        'US$': 'USD', '美元': 'USD', '美金': 'USD',
                        '人民幣': 'CNY', '港幣': 'HKD', '港元': 'HKD'}.get(currency, currency)
            number = Decimal(item['number'].replace(',', ''))
            scale = _SCALES[item['unit'].lower()]
            yield {'number': number, 'value': number * scale, 'currency': currency,
                   'text': match.group().strip()}


def validate_cited_monetary_units(markdown, sources):
    """Flag unchanged mantissas with different scales, not every unmatched figure.

    Only compare amounts of the same currency in explicitly cited paragraphs.
    This is not a general financial fact checker and never rewrites a value.
    """
    inventory = {source.source_id: list(_amounts(source.snippet or '')) for source in sources}
    issues = []
    checked = 0
    seen = set()
    for paragraph in re.split(r'\n\s*\n', markdown):
        refs = list(dict.fromkeys(re.findall(r'\[(S\d+)\]', paragraph)))
        evidence = [(ref, amount) for ref in refs for amount in inventory.get(ref, [])]
        if not evidence:
            continue
        for claim in _amounts(paragraph):
            same_currency = [(ref, amount) for ref, amount in evidence
                             if amount['currency'] == claim['currency']]
            if not same_currency:
                continue
            checked += 1
            # A correctly converted/rounded equivalent in any cited source wins.
            if any(abs(amount['value'] - claim['value']) <= max(Decimal(1), abs(amount['value']) * Decimal('.001'))
                   for _, amount in same_currency):
                continue
            conflicting = [(ref, amount) for ref, amount in same_currency
                           if amount['number'] == claim['number'] and amount['value'] != claim['value']]
            if not conflicting:
                continue
            ref, amount = conflicting[0]
            identity = (claim['text'], ref, amount['text'])
            if identity in seen:
                continue
            seen.add(identity)
            issues.append({'claim': claim['text'], 'source_id': ref, 'source_text': amount['text'],
                           'source_currency': amount['currency'],
                           'source_value_in_yi': str(amount['value'] / Decimal(10)**8),
                           'message': f"金額單位疑似換算錯誤：正文「{claim['text']}」引用 [{ref}]，"
                                      f"但來源明列「{amount['text']}」（相當於 {amount['value'] / Decimal(10)**8:g} 億，幣別 {amount['currency']}）。"
                                      '請核對期間、幣別與單位，勿沿用未換算的尾數。'})
    return {'checked_amounts': checked, 'issues': issues,
            'errors': [item['message'] for item in issues], 'passed': not issues,
            'scope': '僅檢查明確引用段落中同幣別、尾數不變但量級不同的金額；不代表所有財務數值已驗證。'}

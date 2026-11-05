"""Fixed, evidence-bound authorization for the three-case deployment recheck."""
from decimal import Decimal

ROUND = 'hybrid-deploy-recheck-20261105-user6'
LIMITS = {'radar': Decimal('5'), 'news': Decimal('4'), 'topic_maintain': Decimal('4')}


def validate_authorization(record, original_rows):
    if (record.get('round_id') != ROUND or record.get('approved') is not True
            or record.get('total_limit_usd') != '6'
            or record.get('case_limits') != {key: str(value) for key, value in LIMITS.items()}
            or record.get('account_priority') != ['secondary', 'primary']
            or record.get('purpose') != 'development' or not record.get('user_confirmation')
            or record.get('production_state') != 'user_intentionally_stopped'
            or any(record.get(key) is not False for key in ('deployment', 'telegram_send', 'restart', 'paid_deepseek_fallback'))):
        raise ValueError('Three-case authorization does not match the explicit user limits and isolation.')
    unknown = {row['id']: row['estimate'] for row in original_rows if row['status'] == 'unknown'}
    if record.get('retained_unknown') != unknown or any(row['status'] == 'pending' for row in original_rows):
        raise ValueError('Old unknown reservations must match exactly; pending work cannot be ignored.')


def check_ledger_rows(current, original, case, estimate=0, *, acknowledged_unknown_ids=()):
    if case not in LIMITS:
        raise ValueError('Unapproved case')
    by_id = {row['id']: row for row in current}
    if any(by_id.get(row['id']) != row for row in original):
        raise ValueError('Original accounting records changed')
    original_ids = {row['id'] for row in original}
    new = [row for row in current if row['id'] not in original_ids]
    if any(row['purpose'] != 'development' or row['round_id'] != ROUND or row['case_id'] not in LIMITS for row in new):
        raise ValueError('Concurrent or incorrectly classified charge')
    acknowledged = frozenset(acknowledged_unknown_ids)
    eligible = {row['id'] for row in new if row['status'] == 'unknown' and row['actual'] is None
                and row['case_id'] == case}
    if not acknowledged <= eligible:
        raise ValueError('Acknowledged unknown must be an unchanged same-case reservation in this round')
    if any(row['status'] in ('unknown', 'pending') and row['id'] not in acknowledged for row in new):
        raise ValueError('New unknown or pending cost: stop the complete round')
    amount = Decimal(str(estimate))
    if not amount.is_finite() or amount < 0:
        raise ValueError('Invalid reservation')
    spent = sum((Decimal(row['actual'] if row['actual'] is not None else row['estimate']) for row in new), Decimal(0))
    case_spent = sum((Decimal(row['actual'] if row['actual'] is not None else row['estimate']) for row in new if row['case_id'] == case), Decimal(0))
    if spent + amount > Decimal(6) or case_spent + amount > LIMITS[case]:
        raise ValueError('Round or case limit would be exceeded')
    return {'new_rows': new, 'actual_or_reserved': spent, 'case_actual_or_reserved': case_spent}

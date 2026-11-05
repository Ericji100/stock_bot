from decimal import Decimal

import pytest

from tools.hybrid_recheck_guard import ROUND, check_ledger_rows


def test_explicit_topic_unknown_is_still_charged_against_both_caps():
    old = [{'id':'prior','status':'unknown','estimate':'2.79884','actual':None}]
    common = {'purpose':'development','round_id':ROUND}
    rows = [*old, dict(common,id='radar',case_id='radar',status='success',estimate='1',actual='1.026840040'),
            dict(common,id='search',case_id='topic_maintain',status='success',estimate='.1',actual='.04078068'),
            dict(common,id='known',case_id='topic_maintain',status='unknown',estimate='.182326',actual=None)]
    with pytest.raises(ValueError):
        check_ledger_rows(rows, old, 'topic_maintain')
    approved = {'acknowledged_unknown_ids':('known',)}
    result = check_ledger_rows(rows,old,'topic_maintain',Decimal('3.7'),**approved)
    assert result['actual_or_reserved']==Decimal('1.249946720')
    assert result['case_actual_or_reserved']==Decimal('.22310668')
    with pytest.raises(ValueError):
        check_ledger_rows(rows,old,'topic_maintain',Decimal('3.78'),**approved)
    with pytest.raises(ValueError):
        check_ledger_rows(rows,old,'radar',0,**approved)
    for status in ('unknown','pending'):
        newer=dict(common,id='new',case_id='topic_maintain',status=status,estimate='.01',actual=None)
        with pytest.raises(ValueError):
            check_ledger_rows([*rows,newer],old,'topic_maintain',0,**approved)
    with pytest.raises(ValueError):
        check_ledger_rows([{**old[0],'estimate':'0'},*rows[1:]],old,'topic_maintain',0,**approved)


def test_unknown_cannot_be_acknowledged_after_settlement_or_as_pending():
    common={'id':'known','case_id':'topic_maintain','purpose':'development','round_id':ROUND,'estimate':'.182326'}
    for values in ({'status':'success','actual':'.01'},{'status':'pending','actual':None}):
        with pytest.raises(ValueError):
            check_ledger_rows([{**common,**values}],[],'topic_maintain',0,acknowledged_unknown_ids=('known',))

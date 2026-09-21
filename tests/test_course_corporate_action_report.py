import pandas as pd
import pytest

from scripts import course_corporate_action_backtest as bt
from scripts import course_corporate_action_report as report


def trade():
    return bt.open_trade('1234','測試','2026-06-01',100.,90.,{'armed_date':'2026-05-29'},
                         {'sequence':1,'added_date':'2026-05-29'})


def raw():
    return pd.DataFrame([
        {'date':pd.Timestamp('2026-06-03'),'open':85.,'close':86.},
        {'date':pd.Timestamp('2026-07-01'),'open':80.,'close':81.},
        {'date':pd.Timestamp('2026-09-04'),'open':84.,'close':85.},
    ])


def finish(t,closed=True):
    t['status']='CLOSED' if closed else 'OPEN'
    bt.finish_trade(t,'2026-06-03',85.,closed)
    return t


def test_dividend_receivable_not_available_cash_and_not_double_counted():
    t=trade()
    bt.book_action(t,{'date':'2026-06-02','cash':5.,'ratio':1.,'pay_date':'2026-09-15'})
    r=report.account_trade(finish(t),raw())
    assert r['dividend_paid']==0
    assert r['dividend_receivable']==500
    assert r['price_pnl']==-1500
    assert r['gross_pnl']==-1000


def test_later_bonus_release_sold_only_when_available():
    t=trade()
    bt.book_action(t,{'date':'2026-06-02','cash':0.,'ratio':1.1,'pay_date':None})
    r=report.account_trade(finish(t),raw(),{'2026-06-02':{'stock_release_date':'2026-07-01'}})
    assert [s['date']for s in r['sales']]==['2026-06-03','2026-07-01']
    assert r['gross_pnl']==pytest.approx(-700.) # 100*85 + 10*80 - 10000
    assert r['stock_entitlement_settlement_impact']==pytest.approx(-50.)
    assert not r['unsold_tranches']


def test_unverified_bonus_date_is_an_asset_not_a_cash_sale():
    t=trade()
    bt.book_action(t,{'date':'2026-06-02','cash':0.,'ratio':1.1,'pay_date':None})
    r=report.account_trade(finish(t),raw())
    assert len(r['sales'])==1
    assert r['stock_and_rights_value']==pytest.approx(850.)
    assert r['status'].startswith('EXITED_WITH_RIGHTS（')


def test_open_positions_and_paid_dividend_are_included():
    t=trade()
    bt.book_action(t,{'date':'2026-06-02','cash':5.,'ratio':1.,'pay_date':'2026-07-01'})
    r=report.account_trade(finish(t,False),raw())
    assert not r['sales']
    assert r['dividend_paid']==500
    assert r['stock_and_rights_value']==8500
    assert r['gross_pnl']==-1000
    assert r['net_pnl']==-1020


def test_same_day_tranches_only_pay_one_sell_minimum_fee():
    t=trade()
    bt.book_action(t,{'date':'2026-06-02','cash':0.,'ratio':1.1,'pay_date':None})
    r=report.account_trade(finish(t),raw(),{'2026-06-02':{'stock_release_date':'2026-06-03'}})
    assert r['fees_and_transaction_tax']==pytest.approx(20+20+9350*.003)


def test_dividend_unknown_payment_day_is_explicit():
    t=trade()
    bt.book_action(t,{'date':'2026-06-02','cash':5.,'ratio':1.,'pay_date':None})
    r=report.account_trade(finish(t),raw())
    assert r['dividend_payment_unverified']==500
    assert r['dividend_paid']==0 and r['dividend_receivable']==0


def test_summary_reconciles_all_equity_components():
    r=report.account_trade(finish(trade()),raw())
    s=report.summarize([r])
    assert s['sales_value']+s['stock_and_rights_value']+s['dividend_total']-s['cumulative_entry_notional']==s['gross_pnl']
    assert s['gross_pnl']-s['fees_and_transaction_tax']==s['net_pnl']

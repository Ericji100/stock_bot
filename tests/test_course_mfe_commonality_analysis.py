import pandas as pd
import pytest

from scripts.course_mfe_commonality_analysis import future_outcome,condition_counts


def trade():
    return {'code':'1234','name':'測試','entry_date':'2026-06-01','entry_price':100.,
            'initial_defense':90.,'armed_date':'2026-05-29','monitor_added_date':'2026-05-29'}


def bars(n):
    return pd.DataFrame({'date':pd.bdate_range('2026-06-01',periods=n),
                         'open':[100.]*n,'high':[101.]*n,'low':[99.]*n,'close':[100.]*n,'volume':[1000.]*n})


def test_twenty_day_label_excludes_later_peak():
    frame=bars(25);frame.loc[20,'high']=150.
    r=future_outcome(trade(),frame,[])
    assert r['mfe_first20_without_exit']==pytest.approx(1.)
    assert r['first_20pct_bar_without_exit']==21


def test_short_observation_is_censored_not_a_failure():
    r=future_outcome(trade(),bars(10),[])
    assert r['future_bars_available']==10
    assert r['mfe_first20_without_exit'] is None


def test_ex_day_purchase_does_not_receive_same_day_dividend():
    action={'date':'2026-06-01','cash':10.,'ratio':1.,'pay_date':None}
    r=future_outcome(trade(),bars(20),[action])
    assert r['mfe_first20_without_exit']==pytest.approx(1.)


def test_dividend_is_included_in_forward_opportunity_once():
    frame=bars(20)
    frame.loc[1:,'high']=91.
    r=future_outcome(trade(),frame,[{'date':'2026-06-02','cash':10.,'ratio':1.,'pay_date':None}])
    assert r['mfe_first20_without_exit']==pytest.approx(1.)


def test_missing_covariate_not_counted_as_zero():
    rows=[{'target':True,'x':None},{'target':True,'x':1.},
          {'target':False,'x':0.},{'target':False,'x':1.}]
    assert condition_counts(rows,'x',lambda x:x>0)==[(1,1),(1,2)]

"""Compare the service to independently computed common-interval returns."""

import math
import statistics
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.domain import RiskError
from app.schemas.risk import display
from app.services.calculation import calculate_grade
from app.services.risk import RiskService
from conftest import make_prices


@pytest.mark.parametrize('lag',[1,3])
def test_mixed_valid_dates_missing_prices_and_request_order(market, lag):
    cal, now, sessions, repo, universe, _ = market
    end_a = sessions[-1]
    end_b = cal.sessions[cal.sessions.index(end_a)-lag]
    windows = {'005930':sessions,'000660':cal.window(end_b)}
    series = {}
    for ticker, window in windows.items():
        changes = [Decimal((i*7)%17-8)/1000 for i in range(len(window)-1)] if ticker == '000660' else None
        prices = make_prices(window, changes)
        missing = 100 if ticker == '005930' else 200
        prices = [p for i,p in enumerate(prices) if i!=missing]
        series[ticker] = {p.day:p for p in prices}
        repo.replace(calculate_grade(ticker, prices, window, now),list(reversed(prices)))
    weighted=[]
    window=cal.window(end_b)
    for left,right in zip(window,window[1:]):
        if all(left in by and right in by for by in series.values()):
            weighted.append(sum(w*float(series[t][right].close/series[t][left].close-1)
                                for t,w in [('005930',.35),('000660',.65)]))
    expected=display(statistics.stdev(weighted)*math.sqrt(250))
    service=RiskService(repo,cal,universe,now)
    for pairs in [[('005930','.35'),('000660','.65')],[('000660','.65'),('005930','.35')]]:
        request=SimpleNamespace(items=[SimpleNamespace(ticker=t,weight=Decimal(w)) for t,w in pairs])
        if lag == 3:
            # This date's shifted rolling window creates five leading missing
            # intervals; additional disjoint gaps also exceed the 99% budget.
            # A non-expired grade does not waive the existing sample policy.
            with pytest.raises(RiskError) as error:
                service.volatility(request)
            assert error.value.code == 'INSUFFICIENT_SAMPLES'
        else:
            assert service.volatility(request).volatility == expected
    # Failed sync/reanalysis cannot make a four-session-old result usable.
    old_index=cal.sessions.index(end_b)
    assert cal.state(end_b,cal.sessions[old_index+4]) == 'EXPIRED'


def test_ticker_schema_accepts_alphanumeric_krx_code_only_in_upper_case():
    from pydantic import TypeAdapter, ValidationError
    from app.schemas.risk import Ticker
    assert TypeAdapter(Ticker).validate_python('0052D0') == '0052D0'
    for bad in ('0052d0', '005930A', '00593'):
        with pytest.raises(ValidationError):
            TypeAdapter(Ticker).validate_python(bad)

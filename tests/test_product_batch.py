import csv
from dataclasses import replace
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.clients.kis import KisClient
from app.domain import RiskError
from app.services.batch import run_batch
from app.services.calculation import calculate_grade
from app.services.grading_policy import GradingPolicy, product_grade
from app.universe import Instrument, load_universe
from conftest import make_prices


@pytest.mark.parametrize('warning,managed,expected', [('00','N',False), ('01','N',True), ('02','N',True), ('03','N',True), ('00','Y',True)])
def test_live_stock_status_mapping(warning, managed, expected):
    def handler(request):
        if request.url.path == '/oauth2/tokenP':
            return httpx.Response(200,json={'access_token':'synthetic'})
        assert request.headers['tr_id'] == 'FHKST01010100'
        return httpx.Response(200,json={'rt_cd':'0','output':{'stck_shrn_iscd':'005930',
            'mrkt_warn_cls_code':warning,'mang_issu_cls_code':managed}})
    client=KisClient('synthetic','synthetic',transport=httpx.MockTransport(handler))
    try:
        assert client.stock_elevated('005930') is expected
    finally:
        client.close()


def test_unknown_stock_status_is_not_normal():
    def handler(request):
        return httpx.Response(200,json={'access_token':'synthetic','rt_cd':'0','output':{
            'stck_shrn_iscd':'005930','mrkt_warn_cls_code':'99','mang_issu_cls_code':'N'}})
    client=KisClient('synthetic','synthetic',transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RiskError): client.stock_elevated('005930')
    finally: client.close()


def test_stock_batch_persists_classification_and_metrics(market):
    cal,now,sessions,repo,*_=market
    class Client:
        def stock_elevated(self,ticker): return False
        def history(self,*args): return make_prices(sessions,[-.08]*(len(sessions)-1))
    result=run_batch(repo,cal,{'005930':Instrument('005930','test','active','STOCK')},Client(),now,target_date=sessions[-1])
    assert set(result['updated'])=={'005930'}
    with repo.snapshot() as db:
        grade=repo.grades(db)['005930']
        assert grade.risk_grade==2 and grade.var975>1
        assert repo.prices(db,'005930')


def test_new_etf_uses_asset_grade_and_available_history(market):
    cal,now,sessions,repo,*_=market
    inception=sessions[-100]
    instrument=Instrument('069500','test','active','ETF',inception,2,False)
    class Client:
        def etf_metadata(self,ticker):return {'listed_on':inception,'leveraged_or_inverse':False}
        def history(self,ticker,start,end):
            assert start==inception
            return make_prices([d for d in sessions if d>=start])
    result=run_batch(repo,cal,{'069500':instrument},Client(),now,target_date=sessions[-1])
    assert set(result['updated'])=={'069500'}
    with repo.snapshot() as db:
        grade=repo.grades(db)['069500']
        assert grade.risk_grade==2 and grade.price_from>inception


def test_history_proves_maturity_without_fabricating_inception():
    policy=GradingPolicy('ETF',leveraged_or_inverse=False,history_since=date(2020,1,1))
    assert product_grade(.1,policy,date(2026,11,5))==5


def test_missing_fund_metadata_preserves_old_result(market):
    cal,now,sessions,repo,*_=market
    old,_=repo.replace(calculate_grade('069500',make_prices(sessions),sessions,now),make_prices(sessions))
    class Client:
        def etf_metadata(self,ticker):raise RiskError('GRADING_METADATA_REQUIRED','Missing provider metadata')
        def history(self,*args): return make_prices(sessions)
    result=run_batch(repo,cal,{'069500':Instrument('069500','test','active','ETF')},Client(),now,target_date=sessions[-1])
    assert result['failed']==[{'ticker':'069500','code':'GRADING_METADATA_REQUIRED'}]
    with repo.snapshot() as db: assert repo.grades(db)['069500']==old


def test_old_algorithm_checkpoint_requires_reanalysis(market):
    cal,now,sessions,repo,universe,*_=market
    class Client:
        def history(self,*args):return make_prices(sessions)
    run_batch(repo,cal,universe,Client(),now,target_date=sessions[-1])
    assert repo.completed(sessions[-1])==set(universe)
    with repo.connect() as db:
        db.execute("UPDATE analysis_publication SET result_hash='old-algorithm-hash'")
    assert repo.completed(sessions[-1])==set()
    result=run_batch(repo,cal,universe,Client(),now,target_date=sessions[-1])
    assert set(result['updated'])==set(universe)


def test_missing_publication_is_incomplete_and_batch_reprocesses_it(market):
    cal,now,sessions,repo,universe,*_=market
    class Client:
        def history(self,*args):return make_prices(sessions)
    run_batch(repo,cal,universe,Client(),now,target_date=sessions[-1])
    with repo.connect() as db:
        db.execute("DELETE FROM analysis_publication WHERE ticker='005930'")
    assert repo.completed(sessions[-1])=={'000660'}
    result=run_batch(repo,cal,universe,Client(),now,target_date=sessions[-1])
    assert set(result['updated'])=={'005930'}


def test_universe_metadata_validation(tmp_path):
    path=tmp_path/'universe.csv'
    path.write_text('ticker,name,status,kind,inception_date,asset_grade,leveraged_or_inverse\n069500,test,active,ETF,2026-01-01,2,false\n',encoding='utf-8')
    assert load_universe(path)['069500'].asset_grade==2
    path.write_text(path.read_text().replace(',false',',unknown'),encoding='utf-8')
    with pytest.raises(RiskError):load_universe(path)


def test_batch_applies_high_difficulty_limit_to_asset_based_grade(market):
    cal,now,sessions,repo,*_=market
    inception=sessions[-100]
    instrument=Instrument('069500','synthetic','active',kind='FUND',inception=inception,
                          asset_grade=5,high_difficulty=True)
    class Client:
        def history(self,ticker,start,end): return make_prices([d for d in sessions if d>=start])
    result=run_batch(repo,cal,{'069500':instrument},Client(),now,target_date=sessions[-1])
    assert set(result['updated'])=={'069500'}
    with repo.snapshot() as db: assert repo.grades(db)['069500'].risk_grade==2


def test_high_difficulty_metadata_is_parsed_strictly(tmp_path):
    path=tmp_path/'fund.csv'
    path.write_text('ticker,name,status,kind,high_difficulty\n069500,test,active,FUND,true\n',encoding='utf-8')
    assert load_universe(path)['069500'].high_difficulty is True
    path.write_text(path.read_text().replace(',true',',yes'),encoding='utf-8')
    with pytest.raises(RiskError): load_universe(path)


def test_default_universe_declares_high_difficulty_metadata():
    path=Path(__file__).resolve().parents[1]/'config'/'universe.csv'
    with path.open(encoding='utf-8-sig',newline='') as stream:
        rows=list(csv.reader(stream))
    assert 'high_difficulty' in rows[0]
    assert all(len(row)==len(rows[0]) for row in rows[1:])
    loaded=load_universe(path)
    assert loaded and all(not instrument.high_difficulty for instrument in loaded.values())


def test_universe_accepts_alphanumeric_krx_ticker(tmp_path):
    path=tmp_path/'universe.csv'
    path.write_text('ticker,name,status,kind\n0052D0,test,active,STOCK\n',encoding='utf-8')
    assert '0052D0' in load_universe(path)
    path.write_text('ticker,name,status,kind\n0052d0,test,active,STOCK\n',encoding='utf-8')
    with pytest.raises(RiskError):load_universe(path)

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from billing_domain import pack_price, change_mode, new_end, cycle_bounds
from api import billing


@pytest.mark.parametrize('paid,months,reports,quantity,expected',[
    ('28500',1,30,10,'9500.00'),('65000',1,100,10,'6500.00'),
    ('142500',6,30,10,'7916.67'),('325000',6,100,10,'5416.67'),
    ('24500',1,30,10,'8166.67')])
def test_packs_use_price_actually_paid(paid,months,reports,quantity,expected):
    assert pack_price(paid,months,reports,quantity)==Decimal(expected)


@pytest.mark.parametrize('paid',['0','-1','NaN','Infinity'])
def test_price_must_be_known_and_finite(paid):
    with pytest.raises(ValueError): pack_price(paid,1,30,10)


def test_plan_change_timing_and_dates():
    basic={'id':'basic','max_reports_month':30}; plus={'id':'plus','max_reports_month':100}
    assert change_mode(basic,plus)=='exhaustion'
    assert change_mode(plus,basic)=='expiry'
    assert new_end(date(2026,10,15),date(2026,10,30),1)==date(2026,11,14)
    assert new_end(date(2026,10,15),date(2026,10,30),1,'extend_expiry')==date(2026,11,29)
    assert new_end(date(2026,8,31),date(2026,8,31),6)==date(2027,2,28)


def test_managed_monthly_cycle_is_30_days_not_calendar_month():
    n={'subscription_start':'2026-01-31','subscription_end':'2026-03-02',
       'subscription_type':'monthly','billing_current_plan':{'id':'basic'}}
    assert cycle_bounds(n,date(2026,3,1))==(date(2026,1,31),date(2026,3,2))


def test_feature_disabled_never_creates_checkout(monkeypatch):
    monkeypatch.setenv('BILLING_MANAGEMENT_ENABLED','false')
    app=FastAPI(); billing.register_routes(app,lambda:'owner'); client=TestClient(app)
    assert client.get('/api/billing/summary').json()=={'enabled':False}
    assert client.post('/api/billing/orders',json={'request_id':'00000000-0000-0000-0000-000000000001','reports':10}).status_code==503


@pytest.mark.asyncio
async def test_upgrade_pauses_provider_before_marking_paid_ready(monkeypatch):
    db=MagicMock(); calls=[]
    async def provider(method,path,payload=None):
        calls.append((method,payload))
        return {'status':'paused'}
    monkeypatch.setattr(billing,'mp',provider)
    rpc=MagicMock(); monkeypatch.setattr(billing,'rpc',rpc)
    order={'id':'order','kind':'change','mode':'exhaustion','status':'paid','preapproval_id':'mp','nutri_id':'owner'}
    db.client.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data=order
    await billing.sync_order(db,order)
    assert calls==[('PUT',{'status':'paused'}),('GET',None)]
    assert [c.args[1] for c in rpc.call_args_list]==['billing_pause_confirmed','billing_tick']


@pytest.mark.asyncio
async def test_provider_failure_does_not_mark_sync_complete(monkeypatch):
    monkeypatch.setattr(billing,'mp',AsyncMock(side_effect=RuntimeError('network')))
    rpc=MagicMock(); monkeypatch.setattr(billing,'rpc',rpc)
    order={'id':'order','kind':'change','mode':'exhaustion','status':'syncing','preapproval_id':'mp','nutri_id':'owner',
           'target_plan':{'title':'Plus','unit_price':65000,'months':1},'new_end':'2026-11-14'}
    with pytest.raises(RuntimeError): await billing.sync_order(MagicMock(),order)
    rpc.assert_not_called()


@pytest.mark.asyncio
async def test_provider_must_confirm_date_amount_and_frequency(monkeypatch):
    provider=AsyncMock(side_effect=[{}, {'status':'authorized','next_payment_date':'2026-11-30T12:00:00Z',
                    'auto_recurring':{'transaction_amount':65000,'frequency':30,'frequency_type':'days'}}])
    monkeypatch.setattr(billing,'mp',provider)
    rpc=MagicMock(); monkeypatch.setattr(billing,'rpc',rpc)
    order={'id':'order','kind':'change','mode':'exhaustion','status':'syncing','preapproval_id':'mp','nutri_id':'owner',
           'target_plan':{'title':'Plus','unit_price':65000,'months':1},'new_end':'2026-11-14'}
    with pytest.raises(billing.HTTPException,match='502'): await billing.sync_order(MagicMock(),order)
    rpc.assert_not_called()


@pytest.mark.asyncio
async def test_no_catalog_fallback_without_a_paid_receipt(monkeypatch):
    monkeypatch.setattr(billing,'mp',AsyncMock(return_value={'results':[]}))
    with pytest.raises(billing.HTTPException,match='409'):
        await billing.verified_price({'mp_preapproval_id':'mp','subscription_start':'2026-10-01','subscription_end':'2026-10-31'})


@pytest.mark.asyncio
async def test_pending_downgrade_does_not_repeat_provider_changes(monkeypatch):
    provider=AsyncMock(); monkeypatch.setattr(billing,'mp',provider)
    await billing.sync_order(MagicMock(),{'kind':'change','status':'scheduled','provider_synced':True})
    provider.assert_not_called()


@pytest.mark.asyncio
async def test_managed_paid_plan_preserves_its_actual_price(monkeypatch):
    provider=AsyncMock(); monkeypatch.setattr(billing,'mp',provider)
    assert await billing.verified_price({'billing_current_plan':{'id':'plan'},'billing_paid_price':24500})=='24500'
    provider.assert_not_called()


@pytest.mark.parametrize('quantity',[0,1,9,11,20,30,-10])
def test_only_ten_report_packs(quantity):
    with pytest.raises(ValueError): pack_price(28500,1,30,quantity)

@pytest.mark.asyncio
async def test_recurring_payment_routes_through_verified_invoice(monkeypatch):
    from api import subscriptions
    payment={'id':185855908039,'status':'approved'}
    invoice={'id':7033427997,'preapproval_id':'preapproval','payment':payment}
    response=MagicMock(status_code=200); response.json.return_value={'results':[invoice]}
    client=MagicMock(); client.get=AsyncMock(return_value=response)
    context=MagicMock(); context.__aenter__=AsyncMock(return_value=client); context.__aexit__=AsyncMock()
    monkeypatch.setenv('MP_ACCESS_TOKEN','test')
    monkeypatch.setattr(subscriptions.httpx,'AsyncClient',lambda **kw:context)
    handler=AsyncMock(return_value={'ok':True,'detail':'renewal_extended'})
    monkeypatch.setattr(subscriptions,'handle_authorized_payment',handler)
    sdk=MagicMock()
    assert (await subscriptions.handle_recurring_payment(sdk,payment))['ok']
    handler.assert_awaited_once_with(sdk,'7033427997')
    assert client.get.call_args.kwargs['params']=={'payment_id':'185855908039'}

@pytest.mark.asyncio
async def test_unsearchable_invoice_requests_retry(monkeypatch):
    from api import subscriptions
    response=MagicMock(status_code=200); response.json.return_value={'results':[]}
    client=MagicMock(); client.get=AsyncMock(return_value=response)
    context=MagicMock(); context.__aenter__=AsyncMock(return_value=client); context.__aexit__=AsyncMock()
    monkeypatch.setenv('MP_ACCESS_TOKEN','test')
    monkeypatch.setattr(subscriptions.httpx,'AsyncClient',lambda **kw:context)
    with pytest.raises(billing.HTTPException) as exc:
        await subscriptions.handle_recurring_payment(MagicMock(),{'id':1,'status':'approved'})
    assert exc.value.status_code==503

@pytest.mark.asyncio
async def test_provider_outage_requests_retry(monkeypatch):
    from api import subscriptions
    response=MagicMock(status_code=503, text='unavailable')
    client=MagicMock(); client.get=AsyncMock(return_value=response)
    context=MagicMock(); context.__aenter__=AsyncMock(return_value=client); context.__aexit__=AsyncMock()
    monkeypatch.setenv('MP_ACCESS_TOKEN','test')
    monkeypatch.setattr(subscriptions.httpx,'AsyncClient',lambda **kw:context)
    with pytest.raises(billing.HTTPException) as exc:
        await subscriptions.handle_authorized_payment(MagicMock(),'invoice')
    assert exc.value.status_code==502
